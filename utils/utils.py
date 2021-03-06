from __future__ import division
import math
import time
import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches

from shapely.geometry import Polygon
from shapely.geometry import box
from shapely.affinity import rotate
from shapely.validation import explain_validity


def to_cpu(tensor):
    return tensor.detach().cpu()


def load_classes(path):
    """
    Loads class labels at 'path'
    """
    fp = open(path, "r")
    names = fp.read().split("\n")[:-1]
    return names

def load_ms(path):
    """
    Load mean and std devation values from text file
    """
    with open(path, "r") as ms:   ### Read mean and standard deviation
        ms_values = ms.readlines()
        ms_values = [s.strip() for s in ms_values]
        mean_val = [float(s) for s in ms_values[0].split()]
        std_val = [float(s) for s in ms_values[1].split()]
    
    return mean_val, std_val

def write_ms(path, values):
    """
    Write mean and std calues to txt file
    path: To where the file should be stored
    values: list of mean and std [[float],[float]]
    """
    text_file = open(path, 'w')
    for sing in values:
        text_file.writelines( ["%f " % item for item in sing] )
        text_file.write("\n")
    text_file.close()

def weights_init_normal(m):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        torch.nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif classname.find("BatchNorm2d") != -1:
        torch.nn.init.normal_(m.weight.data, 1.0, 0.02)
        torch.nn.init.constant_(m.bias.data, 0.0)


def rescale_boxes(boxes, current_dim, original_shape):
    """ Rescales bounding boxes to the original shape """
    orig_h, orig_w = original_shape
    # The amount of padding that was added
    pad_x = max(orig_h - orig_w, 0) * (current_dim / max(original_shape))
    pad_y = max(orig_w - orig_h, 0) * (current_dim / max(original_shape))
    # Image height and width after padding is removed
    unpad_h = current_dim - pad_y
    unpad_w = current_dim - pad_x
    # Rescale bounding boxes to dimension of original image
    boxes[:, 0] = ((boxes[:, 0] - pad_x // 2) / unpad_w) * orig_w
    boxes[:, 1] = ((boxes[:, 1] - pad_y // 2) / unpad_h) * orig_h
    boxes[:, 2] = ((boxes[:, 2] - pad_x // 2) / unpad_w) * orig_w
    boxes[:, 3] = ((boxes[:, 3] - pad_y // 2) / unpad_h) * orig_h
    return boxes


def xywh2xyxy(x):
    y = x.new(x.shape)
    y[..., 0] = x[..., 0] - x[..., 2] / 2
    y[..., 1] = x[..., 1] - x[..., 3] / 2
    y[..., 2] = x[..., 0] + x[..., 2] / 2
    y[..., 3] = x[..., 1] + x[..., 3] / 2
    return y

def xywh2xyxy_np(x):
    y = np.zeros_like(x)
    y[..., 0] = x[..., 0] - x[..., 2] / 2
    y[..., 1] = x[..., 1] - x[..., 3] / 2
    y[..., 2] = x[..., 0] + x[..., 2] / 2
    y[..., 3] = x[..., 1] + x[..., 3] / 2
    return y

def xyxy2xywh(x):
    y = x.new(x.shape)
    w, h = x[..., 2] - x[..., 0], x[..., 3] - x[..., 1]
    y[...,0] = x[...,0] + (w / 2)
    y[...,1] = x[...,1] + (h / 2)
    y[...,2] = w
    y[...,3] = h
    return y


def ap_per_class(tp, conf, pred_cls, target_cls):
    """ Compute the average precision, given the recall and precision curves.
    Source: https://github.com/rafaelpadilla/Object-Detection-Metrics.
    # Arguments
        tp:    True positives (list).
        conf:  Objectness value from 0-1 (list).
        pred_cls: Predicted object classes (list).
        target_cls: True object classes (list).
    # Returns
        The average precision as computed in py-faster-rcnn.
    """

    # Sort by objectness
    i = np.argsort(-conf)
    tp, conf, pred_cls = tp[i], conf[i], pred_cls[i]

    # Find unique classes
    unique_classes = np.unique(target_cls)

    # Create Precision-Recall curve and compute AP for each class
    ap, p, r = [], [], []
    for c in tqdm.tqdm(unique_classes, desc="Computing AP"):
        i = pred_cls == c
        n_gt = (target_cls == c).sum()  # Number of ground truth objects
        n_p = i.sum()  # Number of predicted objects

        if n_p == 0 and n_gt == 0:
            continue
        elif n_p == 0 or n_gt == 0:
            ap.append(0)
            r.append(0)
            p.append(0)
        else:
            # Accumulate FPs and TPs
            fpc = (1 - tp[i]).cumsum()
            tpc = (tp[i]).cumsum()

            # Recall
            recall_curve = tpc / (n_gt + 1e-16)
            r.append(recall_curve[-1])

            # Precision
            precision_curve = tpc / (tpc + fpc)
            p.append(precision_curve[-1])

            # AP from recall-precision curve
            ap.append(compute_ap(recall_curve, precision_curve))

    # Compute F1 score (harmonic mean of precision and recall)
    p, r, ap = np.array(p), np.array(r), np.array(ap)
    f1 = 2 * p * r / (p + r + 1e-16)

    return p, r, ap, f1, unique_classes.astype("int32")


def compute_ap(recall, precision):
    """ Compute the average precision, given the recall and precision curves.
    Code originally from https://github.com/rbgirshick/py-faster-rcnn.

    # Arguments
        recall:    The recall curve (list).
        precision: The precision curve (list).
    # Returns
        The average precision as computed in py-faster-rcnn.
    """
    # correct AP calculation
    # first append sentinel values at the end
    mrec = np.concatenate(([0.0], recall, [1.0]))
    mpre = np.concatenate(([0.0], precision, [0.0]))

    # compute the precision envelope
    for i in range(mpre.size - 1, 0, -1):
        mpre[i - 1] = np.maximum(mpre[i - 1], mpre[i])

    # to calculate area under PR curve, look for points
    # where X axis (recall) changes value
    i = np.where(mrec[1:] != mrec[:-1])[0]

    # and sum (\Delta recall) * prec
    ap = np.sum((mrec[i + 1] - mrec[i]) * mpre[i + 1])
    return ap


def get_batch_statistics(outputs, targets, iou_threshold, use_angle):
    """ Compute true positives, predicted scores and predicted labels per sample """
    batch_metrics = []
    for sample_i in range(len(outputs)):

        if outputs[sample_i] is None:
            continue

        output = outputs[sample_i]
        pred_boxes = output[:, :5]
        pred_scores = output[:, 5]
        pred_labels = output[:, -1]

        true_positives = np.zeros(pred_boxes.shape[0])

        annotations = targets[targets[:, 0] == sample_i][:, 1:]
        target_labels = annotations[:, 0] if len(annotations) else []
        if len(annotations):
            detected_boxes = []
            target_boxes = annotations[:, 1:]

            for pred_i, (pred_box, pred_label) in enumerate(zip(pred_boxes, pred_labels)):

                # If targets are found break
                if len(detected_boxes) == len(annotations):
                    break

                # Ignore if label is not one of the target labels
                if pred_label not in target_labels:
                    continue

                #iou, box_index = bbox_iou(pred_box.unsqueeze(0), target_boxes).max(0)     # Only checkes once, later if detection with better iou arrives will be ignored
                if use_angle == 'True':
                    iou = iou_rotated(pred_box.unsqueeze(0), target_boxes)
                else:
                    iou = bbox_iou(pred_box.unsqueeze(0), target_boxes)
                mask_matched = (target_labels == pred_label) & (iou >= iou_threshold) 

                iou_matched = torch.where(mask_matched, iou, torch.zeros_like(iou))
                iou_max, box_index = iou_matched.max(0)

                #if iou >= iou_threshold and box_index not in detected_boxes and pred_label == target_labels[box_index]:
                if iou_max >= iou_threshold and box_index not in detected_boxes:
                    true_positives[pred_i] = 1
                    detected_boxes += [box_index]
        batch_metrics.append([true_positives, pred_scores, pred_labels])
    return batch_metrics

def rotate_detections(x1, y1, x2, y2, angle, xyxy=True):

    FloatTensor = torch.cuda.FloatTensor if x1.is_cuda else torch.FloatTensor

    if xyxy:
        w, h = x2 - x1, y2 - y1
        x, y = x1 + w/2, y1 + h/2   
    else:
        # Get the coordinates of bounding boxes
        x, y, w, h = x1, y1, x2, y2 

    # Get co-ordinates for rotated angle
    if not x.size():

        c, s = np.cos(angle/180*np.pi), np.sin(angle/180*np.pi)
        R = np.asarray([[c, s], [-s, c]])
        pts = np.asarray([[-w/2, -h/2], [w/2, -h/2], [w/2, h/2], [-w/2, h/2]])
        rot_pts = []
        for pt in pts:
            rot_pts.append(([x, y] + pt @ R).astype(float))
        contours = FloatTensor([rot_pts[0], rot_pts[1], rot_pts[2], rot_pts[3]])
        
    else:
        contours = []

        for i in range(x.size(0)):
            c, s = np.cos(angle[i]/180*np.pi), np.sin(angle[i]/180*np.pi)
            R = np.asarray([[c, s], [-s, c]])
            pts = np.asarray([[-w[i]/2, -h[i]/2], [w[i]/2, -h[i]/2], [w[i]/2, h[i]/2], [-w[i]/2, h[i]/2]])
            rot_pts = []
            for pt in pts:
                rot_pts.append(([x[i], y[i]] + pt @ R).astype(float))
            contours += [FloatTensor([rot_pts[0], rot_pts[1], rot_pts[2], rot_pts[3]])]

    return contours


def bbox_wh_iou(wh1, wh2):
    wh2 = wh2.t()
    w1, h1 = wh1[0], wh1[1]
    w2, h2 = wh2[0], wh2[1]
    inter_area = torch.min(w1, w2) * torch.min(h1, h2)
    union_area = (w1 * h1 + 1e-16) + w2 * h2 - inter_area
    return inter_area / union_area


def bbox_iou(box1, box2, x1y1x2y2=True):
    """
    Returns the IoU of two bounding boxes
    """
    if not x1y1x2y2:
        # Transform from center and width to exact coordinates
        b1_x1, b1_x2 = box1[:, 0] - box1[:, 2] / 2, box1[:, 0] + box1[:, 2] / 2
        b1_y1, b1_y2 = box1[:, 1] - box1[:, 3] / 2, box1[:, 1] + box1[:, 3] / 2
        b2_x1, b2_x2 = box2[:, 0] - box2[:, 2] / 2, box2[:, 0] + box2[:, 2] / 2
        b2_y1, b2_y2 = box2[:, 1] - box2[:, 3] / 2, box2[:, 1] + box2[:, 3] / 2
    else:
        # Get the coordinates of bounding boxes
        b1_x1, b1_y1, b1_x2, b1_y2 = box1[:, 0], box1[:, 1], box1[:, 2], box1[:, 3]
        b2_x1, b2_y1, b2_x2, b2_y2 = box2[:, 0], box2[:, 1], box2[:, 2], box2[:, 3]

    # get the corrdinates of the intersection rectangle
    inter_rect_x1 = torch.max(b1_x1, b2_x1)
    inter_rect_y1 = torch.max(b1_y1, b2_y1)
    inter_rect_x2 = torch.min(b1_x2, b2_x2)
    inter_rect_y2 = torch.min(b1_y2, b2_y2)
    # Intersection area
    inter_area = torch.clamp(inter_rect_x2 - inter_rect_x1 , min=0) * torch.clamp(
        inter_rect_y2 - inter_rect_y1 , min=0
    )
    # Union Area
    b1_area = (b1_x2 - b1_x1 ) * (b1_y2 - b1_y1 )
    b2_area = (b2_x2 - b2_x1 ) * (b2_y2 - b2_y1 )

    iou = inter_area / (b1_area + b2_area - inter_area + 1e-16)

    return iou

def calculate_rotated(x, y, w, h, angle):
    '''
    angle: degree
    '''
    FloatTensor = torch.cuda.FloatTensor if x.is_cuda else torch.FloatTensor

    w = w.item()
    h = h.item()
    #w, h = w.numpy(), h.numpy()
    c, s = np.cos(angle.item()/180*np.pi), np.sin(angle.item()/180*np.pi)
    R = np.asarray([[c, s], [-s, c]])
    pts = np.asarray([[-w/2, -h/2], [w/2, -h/2], [w/2, h/2], [-w/2, h/2]])
    rot_pts = []
    for pt in pts:
        rot_pts.append(([x, y] + pt @ R).astype(float))
    contours = FloatTensor([rot_pts[0], rot_pts[1], rot_pts[2], rot_pts[3]])
    
    return contours

def iou_rotated(box1, box2, x1y1x2y2=True):

    FloatTensor = torch.cuda.FloatTensor if box1.is_cuda else torch.FloatTensor

    if not x1y1x2y2:
        #Get center co-ordinates and w & h
        b1_cx, b1_cy, b1_w, b1_h = box1[:,0], box1[:,1], box1[:,2], box1[:,3]
        b2_cx, b2_cy, b2_w, b2_h = box2[:,0], box2[:,1], box2[:,2], box2[:,3]
       
    else:
        # Transform co-ordinates to x,y,w,h
        b1_w, b1_h = box1[:,2] - box1[:,0], box1[:,3] - box1[:,1]
        b1_cx, b1_cy = box1[:,0] + b1_w / 2, box1[:,1] + b1_h / 2
        
        b2_w, b2_h = box2[:,2] - box2[:,0], box2[:,3] - box2[:,1]
        b2_cx, b2_cy = box2[:,0] + b2_w / 2, box2[:,1] + b2_h / 2
        
    
    #get angle for rotation for all bounding boxes
    angle_1 = box1[:,-1]
    angle_2 = box2[:,-1]

    if len(box1) == 1:
        iou_all = FloatTensor(box2.size(0)).fill_(0)
        for i in range(len(box2)):
            #Check if any element equals to infinity
            if box1[0,0]==np.inf  or box1[0,1]==np.inf or box1[0,2]==np.inf or box1[0,3]==np.inf \
            or box1[0,0]==np.nan  or box1[0,1]==np.nan or box1[0,2]==np.nan or box1[0,3]==np.nan:
                iou = 1e-12
            else:
                rot_box1 = calculate_rotated(b1_cx[0], b1_cy[0], b1_w[0], b1_h[0], angle_1[0])
                rot_box2 = calculate_rotated(b2_cx[i], b2_cy[i], b2_w[i], b2_h[i], angle_2[i])

                # b1_x1, b1_y1 = rot_box1.min(0)[0]
                # b1_x2, b1_y2 = rot_box1.max(0)[0]
                # b2_x1, b2_y1 = rot_box2.min(0)[0]
                # b2_x2, b2_y2 = rot_box2.max(0)[0]

                # # get the co-ordinates of the intersection rectangle
                # inter_rect_x1 = torch.max(b1_x1, b2_x1)
                # inter_rect_y1 = torch.max(b1_y1, b2_y1)
                # inter_rect_x2 = torch.min(b1_x2, b2_x2)
                # inter_rect_y2 = torch.min(b1_y2, b2_y2)
                # # Intersection area
                # inter_area = torch.clamp(inter_rect_x2 - inter_rect_x1 + 1, min=0) * torch.clamp(
                #     inter_rect_y2 - inter_rect_y1 + 1, min=0
                # )
                # # Union Area
                # b1_area = (b1_x2 - b1_x1 + 1) * (b1_y2 - b1_y1 + 1)
                # b2_area = (b2_x2 - b2_x1 + 1) * (b2_y2 - b2_y1 + 1)

                # iou = inter_area / (b1_area + b2_area - inter_area + 1e-16)

                try:
                    rot_box1 = Polygon( [ rot_box1[0], rot_box1[1], rot_box1[2], rot_box1[3] ] )
                    rot_box2 = Polygon( [ rot_box2[0], rot_box2[1], rot_box2[2], rot_box2[3] ] )

                    if rot_box1.is_valid == False or rot_box2.is_valid == False:
                        rot_box1 = rot_box1.buffer(0)
                        rot_box2 = rot_box2.buffer(0)

                    # Intersection area
                    inter_area = rot_box1.intersection(rot_box2).area
                    # Union Area
                    union_area = rot_box1.union(rot_box2).area

                    iou = inter_area / (union_area + 1e-9)
                except Exception as inst:
                    #print(inst)
                    iou = 1e-9
                    pass

            iou_all[i] = iou

        return iou_all

    else:
        assert(len(box1) == len(box2))
        #rotate every bbox
        iou_all = FloatTensor(box1.size(0)).fill_(0)
        for i in range(len(box1)):
            #Check if any element equals to infinity
            if box1[i,0]==np.inf or box1[i,1]==np.inf or box1[i,2]==np.inf or box1[i,3]==np.inf \
            or box1[i,0]==np.nan or box1[i,1]==np.nan or box1[i,2]==np.nan or box1[i,3]==np.nan:
                iou = 1e-12
            else:
                rot_box1 = calculate_rotated(b1_cx[i], b1_cy[i], b1_w[i], b1_h[i], angle_1[i])
                rot_box2 = calculate_rotated(b2_cx[i], b2_cy[i], b2_w[i], b2_h[i], angle_2[i])

                # b1_x1, b1_y1 = rot_box1.min(0)[0]
                # b1_x2, b1_y2 = rot_box1.max(0)[0]
                # b2_x1, b2_y1 = rot_box2.min(0)[0]
                # b2_x2, b2_y2 = rot_box2.max(0)[0]

                # # get the co-ordinates of the intersection rectangle
                # inter_rect_x1 = torch.max(b1_x1, b2_x1)
                # inter_rect_y1 = torch.max(b1_y1, b2_y1)
                # inter_rect_x2 = torch.min(b1_x2, b2_x2)
                # inter_rect_y2 = torch.min(b1_y2, b2_y2)
                # # Intersection area
                # inter_area = torch.clamp(inter_rect_x2 - inter_rect_x1 + 1, min=0) * torch.clamp(
                #     inter_rect_y2 - inter_rect_y1 + 1, min=0
                # )
                # # Union Area
                # b1_area = (b1_x2 - b1_x1 + 1) * (b1_y2 - b1_y1 + 1)
                # b2_area = (b2_x2 - b2_x1 + 1) * (b2_y2 - b2_y1 + 1)

                # iou = inter_area / (b1_area + b2_area - inter_area + 1e-16)

                try:
                    rot_box1 = Polygon( [ rot_box1[0], rot_box1[1], rot_box1[2], rot_box1[3] ] )
                    rot_box2 = Polygon( [ rot_box2[0], rot_box2[1], rot_box2[2], rot_box2[3] ] )

                    if rot_box1.is_valid == False or rot_box2.is_valid == False:
                        rot_box1 = rot_box1.buffer(0)
                        rot_box2 = rot_box2.buffer(0)
                    # Intersection area
                    inter_area = rot_box1.intersection(rot_box2).area
                    # Union Area
                    union_area = rot_box1.union(rot_box2).area

                    iou = inter_area / union_area
                except Exception as inst:
                    #print(inst)
                    iou = 1e-9
                    pass


            iou_all[i] = iou

        return iou_all


def non_max_suppression(prediction, use_angle, conf_thres=0.5, nms_thres=0.4):
    """
    Removes detections with lower object confidence score than 'conf_thres' and performs
    Non-Maximum Suppression to further filter detections.
    Returns detections with shape:
        (x1, y1, x2, y2, object_conf, class_score, class_pred)
    """

    # From (center x, center y, width, height) to (x1, y1, x2, y2)
    prediction[..., :4] = xywh2xyxy(prediction[..., :4])
    output = [None for _ in range(len(prediction))]
    for image_i, image_pred in enumerate(prediction):
        # Filter out confidence scores below threshold
        image_pred = image_pred[image_pred[:, 5] >= conf_thres]
        # If none are remaining => process next image
        if not image_pred.size(0):
            continue
        # Object confidence times class confidence
        score = image_pred[:, 5] * image_pred[:, 6:].max(1)[0]
        # Sort by it
        image_pred = image_pred[(-score).argsort()]
        class_confs, class_preds = image_pred[:, 6:].max(1, keepdim=True)
        detections = torch.cat((image_pred[:, :6], class_confs.float(), class_preds.float()), 1)

    #     ##### takes boxes for filtering
    #     boxes = detections[:,0:5] # only [x,y,w,h,a]
    #     valid = torch.zeros(boxes.shape[0], dtype=torch.bool)
    #     # the first one is always valid
    #     valid[0] = True
    #     # only one candidate at the beginning. Its votes number is 1 (it self)
    #     votes = [1]
    #     for i in range(1, boxes.shape[0]):
    #     # compute IoU with valid boxes

    #         ious = iou_rotated(boxes[i].unsqueeze(0), boxes[valid,:])
    #         if (ious >= nms_thres).any():
    #             continue
    #         # else, this box is valid
    #         valid[i] = True
    #         # the votes number of the new candidate BB is 1 (it self)
    #         votes.append(1)
    #     selected = detections[valid,:]

    #     if selected:
    #         output[image_i] = torch.stack(selected)

    # return selected

        
        # Perform non-maximum suppression
        keep_boxes = []
        while detections.size(0):
            if use_angle == 'True':
                large_overlap = iou_rotated(detections[0, :5].unsqueeze(0), detections[:, :5]) > nms_thres
            else:
                large_overlap = bbox_iou(detections[0, :4].unsqueeze(0), detections[:, :4]) > nms_thres
            label_match = detections[0, -1] == detections[:, -1]
            # Indices of boxes with lower confidence scores, large IOUs and matching labels
            invalid = large_overlap & label_match
            weights = detections[invalid, 5:6]
            # Merge overlapping bboxes by order of confidence
            detections[0, :4] = (weights * detections[invalid, :4]).sum(0) / weights.sum()
            keep_boxes += [detections[0]]
            detections = detections[~invalid]
        if keep_boxes:
            output[image_i] = torch.stack(keep_boxes)
    
    for o_i, out in enumerate(output):
        if out == None:
            output[o_i] = torch.zeros(1,8)
            

    return output


def build_targets(pred_boxes, pred_cls, target, anchors, ignore_thres, use_angle):

    ByteTensor = torch.cuda.BoolTensor if pred_boxes.is_cuda else torch.BoolTensor
    FloatTensor = torch.cuda.FloatTensor if pred_boxes.is_cuda else torch.FloatTensor

    nB = pred_boxes.size(0)
    nA = pred_boxes.size(1)
    nC = pred_cls.size(-1)
    nG = pred_boxes.size(2)
    nt = target.size(0)

    # Output tensors
    obj_mask = ByteTensor(nB, nA, nG, nG).fill_(0)
    noobj_mask = ByteTensor(nB, nA, nG, nG).fill_(1)
    class_mask = FloatTensor(nB, nA, nG, nG).fill_(0)
    iou_scores = FloatTensor(nB, nA, nG, nG).fill_(0)
    tx = FloatTensor(nB, nA, nG, nG).fill_(0)
    ty = FloatTensor(nB, nA, nG, nG).fill_(0)
    tw = FloatTensor(nB, nA, nG, nG).fill_(0)
    th = FloatTensor(nB, nA, nG, nG).fill_(0)
    tangle = FloatTensor(nB, nA, nG, nG).fill_(0)
    tcls = FloatTensor(nB, nA, nG, nG, nC).fill_(0)
    target_boxes = FloatTensor(nt,5).fill_(0)

    # Convert to position relative to box
    target_boxes[:,:4] = target[:, 2:6] * nG
    target_boxes[:,4] = target[:, 6]
    gxy = target_boxes[:, :2]
    gwh = target_boxes[:, 2:4]
    gangle = target_boxes[:, 4]
    # Get anchors with best iou
    ious = torch.stack([bbox_wh_iou(anchor, gwh) for anchor in anchors])
    best_ious, best_n = ious.max(0)
    # Separate target values
    b, target_labels = target[:, :2].long().t()
    gx, gy = gxy.t()
    gw, gh = gwh.t()
    gi, gj = gxy.long().t()
    # Set masks
    obj_mask[b, best_n, gj, gi] = 1
    noobj_mask[b, best_n, gj, gi] = 0

    # Set noobj mask to zero where iou exceeds ignore threshold
    for i, anchor_ious in enumerate(ious.t()):
        noobj_mask[b[i], anchor_ious > ignore_thres, gj[i], gi[i]] = 0

    # Coordinates
    tx[b, best_n, gj, gi] = gx - gx.floor()
    ty[b, best_n, gj, gi] = gy - gy.floor()
    # Width and height
    tw[b, best_n, gj, gi] = torch.log(gw / anchors[best_n][:, 0] + 1e-16)
    th[b, best_n, gj, gi] = torch.log(gh / anchors[best_n][:, 1] + 1e-16)
    # Angle
    tangle[b, best_n, gj, gi] = gangle
    # One-hot encoding of label
    tcls[b, best_n, gj, gi, target_labels] = 1
    # Compute label correctness and iou at best anchor
    class_mask[b, best_n, gj, gi] = (pred_cls[b, best_n, gj, gi].argmax(-1) == target_labels).float()
    if use_angle == 'True':
        iou_scores[b, best_n, gj, gi] = iou_rotated(pred_boxes[b, best_n, gj, gi], target_boxes, x1y1x2y2=False)
    else:
        iou_scores[b, best_n, gj, gi] = bbox_iou(pred_boxes[b, best_n, gj, gi], target_boxes, x1y1x2y2=False)

    tconf = obj_mask.float()
    return iou_scores, class_mask, obj_mask, noobj_mask, tx, ty, tw, th, tangle, tcls, tconf
