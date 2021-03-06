import numpy as np
import torch
import warnings
from PIL import Image, ImageDraw, ImageFont
import torchvision.transforms as transforms
import colorsys
import random

"""
Transform (x, y, w, h) into (x1, y1, x2, y2)
"""
def whToxy(box, reversed=False):
    box_ = box.new_empty(box.shape)
    if reversed:
        box_[..., 0] = box[..., 1] - box[..., 3] / 2
        box_[..., 1] = box[..., 0] - box[..., 2] / 2
        box_[..., 2] = box[..., 1] + box[..., 3] / 2
        box_[..., 3] = box[..., 0] + box[..., 2] / 2
    else:
        box_[..., 0] = box[..., 0] - box[..., 2] / 2
        box_[..., 1] = box[..., 1] - box[..., 3] / 2
        box_[..., 2] = box[..., 0] + box[..., 2] / 2
        box_[..., 3] = box[..., 1] + box[..., 3] / 2
    return box_

"""
calculate iou of two boxes
box1: (x1, y1, x2, y2)
box2: (x1, y1, x2, y2)
"""
def iou(box1, box2):
    box1_x1, box1_y1, box1_x2, box1_y2 = box1[...,0], box1[...,1], box1[...,2], box1[...,3]
    box2_x1, box2_y1, box2_x2, box2_y2 = box2[...,0], box2[...,1], box2[...,2], box2[...,3]

    x1 = torch.max(box1_x1, box2_x1)
    y1 = torch.max(box1_y1, box2_y1)
    x2 = torch.min(box1_x2, box2_x2)
    y2 = torch.min(box1_y2, box2_y2) 

    ins_area = (x2 - x1).clamp(min=0) * (y2 - y1).clamp(min=0)
    uni_area = (
        (box1_x1 - box1_x2) * (box1_y1 - box1_y2) + 
        (box2_x1 - box2_x2) * (box2_y1 - box2_y2)
        - ins_area
    )

    iou_scores = ins_area / uni_area
    return iou_scores

"""
Read the classes names
"""
def get_classes(file_path):
    classes = []
    with open(file_path, 'r')  as f:
        for line in f.readlines():
            line = line.rstrip()
            classes.append(line)
    return classes

"""
Find detector in YOLO where ground truth box should appear.
Parameters
----------
labels : array
    List of ground truth boxes in form of relative x, y, w, h, class.
    Relative coordinates are in the range [0, 1] indicating a percentage
    of the original image dimensions.
anchors : array
    List of anchors in unit of initial image_size in the range [0, 1]
Returns
-------
matching_true_boxes: tensor, [B, H, W, A, num_cls + 5]
-----------
Ref: https://github.com/allanzelener/YAD2K
"""
def preprocess_true_boxes(labels, anchors, grid_size, device, num_cls=80):
    W, H = grid_size
    A = len(anchors)
    B = len(labels) 
    matching_true_boxes = np.zeros((B, H, W, A, 5+num_cls), dtype=np.float32)
    labels_ = labels.copy()
    labels_[..., 0:2] = labels_[..., 0:2] * np.array([H, W]).reshape(1, 2)
    for b in range(B):
        for box in labels_[b]:
            i = int(box[0])
            j = int(box[1])
            box_wh = box[2:4].reshape(1, 2)
            min_wh = np.minimum(box_wh, anchors)
            ins_area = min_wh[..., 0] * min_wh[..., 1]
            uni_area = (
                box_wh[..., 0] * box_wh[..., 1] 
                + anchors[..., 0] * anchors[..., 1]
                - ins_area
            )
            iou_scores = ins_area / (uni_area + 1e-8)
            idx = np.argmax(iou_scores, axis=-1)

            matching_true_boxes[b, i, j, idx, 0] = box[0] / H
            matching_true_boxes[b, i, j, idx, 1] = box[1] / W
            matching_true_boxes[b, i, j, idx, 2] = box[2]
            matching_true_boxes[b, i, j, idx, 3] = box[3]
            matching_true_boxes[b, i, j, idx, 4] = 1
            assert len(box) >= 4
            # for multi-label
            for k in box[4:]:
                matching_true_boxes[b, i, j, idx, int(k)+5] = 1

    matching_true_boxes = torch.from_numpy(matching_true_boxes).to(device)
    return matching_true_boxes

"""
Non max suppression to filter predicted boxes
Inputs:
boxes: tensor
        shape->[N, 4], corrd format(x1, y1, x2, y2)
scores: tensor
        shape->[N,]
max_output_size: int
        max number of boxes to be selected by nms
iou_threshold: float
        the threshold deciding whether boxes overlaps too much w/ respect to iou
Returns:
selected_indices: LongTensor
        shape->[M,], M <= N
"""
def nms(boxes, scores, device, iou_threshold=0.5, max_output_size=None):
    assert len(boxes) == len(scores)
    if len(scores) == 0:
        warnings.warn("No boxes need to be filtered by nms")
        return torch.LongTensor([]).to(device)

    x1 = boxes[..., 0]
    y1 = boxes[..., 1]
    x2 = boxes[..., 2]
    y2 = boxes[..., 3]
    area = (x2 - x1) * (y2- y1) # shape [N,]
    assert (area > 0).all()
    _, indices = torch.sort(scores, descending=True)
    selected_indices = []
    flag = 0 # to track num of output
    if max_output_size is None:
        max_output_size = len(scores) + 1

    while indices.numel() > 0:
        idx = indices[0]
        flag += 1
        remain_idx = indices[1:]
        selected_indices.append(idx.item())
        # make sure no empty tensor in remain_idx
        if remain_idx.numel() == 0:
            break

        x1_ = torch.max(x1[remain_idx], x1[idx])
        y1_ = torch.max(y1[remain_idx], y1[idx])
        x2_ = torch.min(x2[remain_idx], x2[idx])
        y2_ = torch.min(y2[remain_idx], y2[idx])
        ins = (x2_ - x1_).clamp(min=0) * (y2_ - y1_).clamp(min=0)
        iou = ins / (area[idx] + area[remain_idx] - ins)
        keep_mask = iou < iou_threshold
        indices = remain_idx[keep_mask]

        if torch.sum(keep_mask) == 0:
            break
        if flag >= max_output_size:
            break 
    
    return torch.LongTensor(selected_indices).to(device)


"""
Preprocess input image to match Model input shape
And add batch dimension
Default Model Input shape is (416, 416)
return: image-> tensor, channel-first
        image_size-> (width, height)
        im-> PIL format
"""
def preprocess_image(image_path, input_shape=(416, 416)):
    im = Image.open(image_path)
    image_size = im.size
    im_ = im.resize(input_shape)
    image = transforms.ToTensor()(im_)
    c, h, w = image.shape
    image = image.view(1, c, h, w)
    return im, image, image_size

"""
Following Code to draw boxes are from github repo: 
https://github.com/allanzelener/YAD2K
"""

def generate_colors(class_names):
    hsv_tuples = [(x / len(class_names), 1., 1.) for x in range(len(class_names))]
    colors = list(map(lambda x: colorsys.hsv_to_rgb(*x), hsv_tuples))
    colors = list(map(
        lambda x: (int(x[0] * 255), int(x[1] * 255), int(x[2] * 255)), colors))
    random.seed(10101)  # Fixed seed for consistent colors across runs.
    random.shuffle(colors)  # Shuffle colors to decorrelate adjacent classes.
    random.seed(None)  # Reset seed to default.
    return colors

def draw_boxes(image, out_scores, out_boxes, out_classes, class_names, colors):
    
    font = ImageFont.truetype(
        font='data/FiraMono-Medium.otf',size=np.floor(3e-2 * image.size[1] + 0.5).astype('int32'))
    thickness = (image.size[0] + image.size[1]) // 300

    for i, c in reversed(list(enumerate(out_classes))):
        predicted_class = class_names[c]
        box = out_boxes[i]
        score = out_scores[i]

        label = '{} {:.2f}'.format(predicted_class, score)

        draw = ImageDraw.Draw(image)
        label_size = draw.textsize(label, font)

        top, left, bottom, right = box
        top = max(0, np.floor(top + 0.5).astype('int32'))
        left = max(0, np.floor(left + 0.5).astype('int32'))
        bottom = min(image.size[1], np.floor(bottom + 0.5).astype('int32'))
        right = min(image.size[0], np.floor(right + 0.5).astype('int32'))

        if top - label_size[1] >= 0:
            text_origin = np.array([left, top - label_size[1]])
        else:
            text_origin = np.array([left, top + 1])

        # My kingdom for a good redistributable image drawing library.
        for i in range(thickness):
            draw.rectangle([left + i, top + i, right - i, bottom - i], outline=colors[c])
        draw.rectangle([tuple(text_origin), tuple(text_origin + label_size)], fill=colors[c])
        draw.text(text_origin, label, fill=(0, 0, 0), font=font)
        del draw