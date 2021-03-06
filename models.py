from __future__ import division

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np

from utils.parse_config import *
from utils.utils import build_targets, to_cpu, non_max_suppression

import matplotlib.pyplot as plt
import matplotlib.patches as patches


def create_modules(module_defs):
    """
    Constructs module list of layer blocks from module configuration in module_defs
    """
    hyperparams = module_defs.pop(0)
    output_filters = [int(hyperparams["channels"])]
    module_list = nn.ModuleList()
    for module_i, module_def in enumerate(module_defs):
        modules = nn.Sequential()

        if module_def["type"] == "convolutional":
            bn = int(module_def["batch_normalize"])
            filters = int(module_def["filters"])
            kernel_size = int(module_def["size"])
            pad = (kernel_size - 1) // 2
            modules.add_module(
                f"conv_{module_i}",
                nn.Conv2d(
                    in_channels=output_filters[-1],
                    out_channels=filters,
                    kernel_size=kernel_size,
                    stride=int(module_def["stride"]),
                    padding=pad,
                    bias=not bn,
                ),
            )
            if bn:
                modules.add_module(f"batch_norm_{module_i}", nn.BatchNorm2d(filters, momentum=0.9, eps=1e-5))
            if module_def["activation"] == "leaky":
                modules.add_module(f"leaky_{module_i}", nn.LeakyReLU(0.1))

        elif module_def["type"] == "maxpool":
            kernel_size = int(module_def["size"])
            stride = int(module_def["stride"])
            if kernel_size == 2 and stride == 1:
                modules.add_module(f"_debug_padding_{module_i}", nn.ZeroPad2d((0, 1, 0, 1)))
            maxpool = nn.MaxPool2d(kernel_size=kernel_size, stride=stride, padding=int((kernel_size - 1) // 2))
            modules.add_module(f"maxpool_{module_i}", maxpool)

        elif module_def["type"] == "upsample":
            upsample = Upsample(scale_factor=int(module_def["stride"]), mode="nearest")
            modules.add_module(f"upsample_{module_i}", upsample)

        elif module_def["type"] == "route":
            layers = [int(x) for x in module_def["layers"].split(",")]
            filters = sum([output_filters[1:][i] for i in layers])
            modules.add_module(f"route_{module_i}", EmptyLayer())

        elif module_def["type"] == "shortcut":
            filters = output_filters[1:][int(module_def["from"])]
            modules.add_module(f"shortcut_{module_i}", EmptyLayer())

        elif module_def["type"] == "yolo":
            anchor_idxs = [int(x) for x in module_def["mask"].split(",")]
            # Extract anchors
            anchors = [int(x) for x in module_def["anchors"].split(",")]
            anchors = [(anchors[i], anchors[i + 1]) for i in range(0, len(anchors), 2)]
            anchors = [anchors[i] for i in anchor_idxs]
            num_classes = int(module_def["classes"])
            img_size = int(hyperparams["height"])
            # Define detection layer
            yolo_layer = YOLOLayer(anchors, num_classes, img_size)
            modules.add_module(f"yolo_{module_i}", yolo_layer)
        # Register module list and number of output filters
        module_list.append(modules)
        output_filters.append(filters)

    return hyperparams, module_list


class Upsample(nn.Module):
    """ nn.Upsample is deprecated """

    def __init__(self, scale_factor, mode="nearest"):
        super(Upsample, self).__init__()
        self.scale_factor = scale_factor
        self.mode = mode

    def forward(self, x):
        x = F.interpolate(x, scale_factor=self.scale_factor, mode=self.mode)
        return x


class EmptyLayer(nn.Module):
    """Placeholder for 'route' and 'shortcut' layers"""

    def __init__(self):
        super(EmptyLayer, self).__init__()


class YOLOLayer(nn.Module):
    """Detection layer"""

    def __init__(self, anchors, num_classes, img_dim=416):
        super(YOLOLayer, self).__init__()
        self.anchors = anchors
        self.num_anchors = len(anchors)
        self.num_classes = num_classes
        self.ignore_thres = 0.5
        self.mse_loss = nn.MSELoss()
        self.bce_loss = nn.BCELoss()
        self.obj_scale = 1
        self.noobj_scale = 100
        self.metrics = {}
        self.img_dim = img_dim
        self.grid_size = 0  # grid size
        self.angle_range = 360   # 180 or 360
        self.rot_l1 = nn.L1Loss(reduction='sum')
        self.entropy_lambda = 0.0001  ## 0.001
        self.uda_metrics = {}


    def rotation_loss(self,pred_angle,actual_angle):
        theta_pred = pred_angle
        theta_gt = actual_angle
        dt = theta_pred - theta_gt

        # periodic SE
        dt = torch.abs(torch.remainder(dt-np.pi/2,np.pi) - np.pi/2)

        assert (dt >= 0).all()
        loss = dt.sum()

        return loss

    def entropy_loss(self, feature_map):
        """
        feature_map: s, k, w, h, c 
        output: entropy loss
        """
        # assert feature_map.dim() == 5
        # n, k, h, w, c = feature_map.size()

        # entropy_map = torch.sum( - torch.mul(feature_map, torch.log2(feature_map + 1e-30)), dim=4)  / np.log2(c)  # for single class
        # loss = torch.sum(entropy_map) / (n * h * w *k )  ## divide by total number of anchors

        ### minent20
        assert feature_map.dim() == 2
        s, c = feature_map.size()

        entropy_map = torch.sum( - torch.mul(feature_map, torch.log2(feature_map + 1e-30)), dim=1)  / np.log2(c)
        loss = torch.sum(entropy_map) / (s) 

        return loss

    def compute_grid_offsets(self, grid_size, cuda=True):
        self.grid_size = grid_size
        g = self.grid_size
        FloatTensor = torch.cuda.FloatTensor if cuda else torch.FloatTensor
        self.stride = self.img_dim / self.grid_size
        # Calculate offsets for each grid
        self.grid_x = torch.arange(g).repeat(g, 1).view([1, 1, g, g]).type(FloatTensor)
        self.grid_y = torch.arange(g).repeat(g, 1).t().view([1, 1, g, g]).type(FloatTensor)
        self.scaled_anchors = FloatTensor([(a_w / self.stride, a_h / self.stride) for a_w, a_h in self.anchors])
        self.anchor_w = self.scaled_anchors[:, 0:1].view((1, self.num_anchors, 1, 1))
        self.anchor_h = self.scaled_anchors[:, 1:2].view((1, self.num_anchors, 1, 1))

    def forward(self, x, use_angle, uda_method, targets=None, img_dim=None, ):

        # Tensors for cuda support
        FloatTensor = torch.cuda.FloatTensor if x.is_cuda else torch.FloatTensor
        LongTensor = torch.cuda.LongTensor if x.is_cuda else torch.LongTensor
        ByteTensor = torch.cuda.ByteTensor if x.is_cuda else torch.ByteTensor

        self.img_dim = img_dim
        num_samples = x.size(0)
        grid_size = x.size(2)

        prediction = (
            x.view(num_samples, self.num_anchors, self.num_classes + 6, grid_size, grid_size)
            .permute(0, 1, 3, 4, 2)
            .contiguous()
        )

        # if uda_method:
        ### minent17
        #     feat_map = x.clone()
        #     feat_map = (feat_map.view(num_samples, self.num_anchors, self.num_classes + 6, grid_size, grid_size)
        #     .permute(0, 3, 4, 1, 2).contiguous())
        #     feat_map = torch.nn.functional.softmax(feat_map, dim=3)

        # Get outputs
        x = torch.sigmoid(prediction[..., 0])  # Center x
        y = torch.sigmoid(prediction[..., 1])  # Center y
        w = prediction[..., 2]  # Width
        h = prediction[..., 3]  # Height
        angle = torch.sigmoid(prediction[...,4])
        pred_conf = torch.sigmoid(prediction[..., 5])  # Conf
        pred_cls = torch.sigmoid(prediction[..., 6:])  # Cls pred.   ### Changes for single class
        
        # If grid size does not match current we compute new offsets
        if grid_size != self.grid_size:
            self.compute_grid_offsets(grid_size, cuda=x.is_cuda)

        # Add offset and scale with anchors
        pred_boxes = FloatTensor(prediction[..., :5].shape)
        pred_boxes[..., 0] = x.data + self.grid_x
        pred_boxes[..., 1] = y.data + self.grid_y
        pred_boxes[..., 2] = torch.exp(w.data) * self.anchor_w
        pred_boxes[..., 3] = torch.exp(h.data) * self.anchor_h
        if use_angle == 'True':
            pred_boxes[..., 4] =   angle * self.angle_range - (self.angle_range / 2)
        else:
            pred_boxes[...,4]  = 0

        pred_boxes_out = pred_boxes.detach().clone()
        pred_boxes_out[...,:4] = pred_boxes_out[...,:4] * self.stride

        output = torch.cat(
            (
                pred_boxes_out.view(num_samples, -1, 5) ,
                pred_conf.view(num_samples, -1, 1),
                pred_cls.view(num_samples, -1, self.num_classes),
            ),
            -1,
        )

        if uda_method is None:
            if targets is None:
                return output, 0
            else:
                iou_scores, class_mask, obj_mask, noobj_mask, tx, ty, tw, th, tangle, tcls, tconf = build_targets(
                    pred_boxes=pred_boxes,
                    pred_cls=pred_cls,
                    target=targets,
                    anchors=self.scaled_anchors,
                    ignore_thres=self.ignore_thres,
                    use_angle=use_angle,
                )

                # Convert both the angles to radian for loss calculation
                tangle_mask = tangle[obj_mask] / 180 * np.pi
                if self.angle_range == 360:
                    pangle_mask = angle[obj_mask] * 2 * np.pi - np.pi
                elif self.angle_range == 180:
                    pangle_mask = angle[obj_mask] * np.pi - np.pi / 2

                # Loss : Mask outputs to ignore non-existing objects (except with conf. loss)
                loss_x = self.mse_loss(x[obj_mask], tx[obj_mask])
                loss_y = self.mse_loss(y[obj_mask], ty[obj_mask])
                loss_w = self.mse_loss(w[obj_mask], tw[obj_mask])
                loss_h = self.mse_loss(h[obj_mask], th[obj_mask])
                loss_conf_obj = self.bce_loss(pred_conf[obj_mask], tconf[obj_mask])
                loss_conf_noobj = self.bce_loss(pred_conf[noobj_mask], tconf[noobj_mask])
                loss_conf = self.obj_scale * loss_conf_obj + self.noobj_scale * loss_conf_noobj
                loss_cls = self.bce_loss(pred_cls[obj_mask], tcls[obj_mask])
                if use_angle == 'True':
                    loss_a = self.rotation_loss(pangle_mask, tangle_mask)
                    #loss_a = self.rot_l1(pangle_mask, tangle_mask)
                    total_loss = loss_x + loss_y + loss_w + loss_h + loss_conf + loss_cls + 0.2*loss_a
                else:
                    total_loss = loss_x + loss_y + loss_w + loss_h + loss_conf + loss_cls
                # Metrics
                cls_acc = 100 * class_mask[obj_mask].mean()
                conf_obj = pred_conf[obj_mask].mean()
                conf_noobj = pred_conf[noobj_mask].mean()
                conf50 = (pred_conf > 0.5).float()
                iou50 = (iou_scores > 0.5).float()
                iou75 = (iou_scores > 0.75).float()
                detected_mask = conf50 * class_mask * tconf
                precision = torch.sum(iou50 * detected_mask) / (conf50.sum() + 1e-16)
                recall50 = torch.sum(iou50 * detected_mask) / (obj_mask.sum() + 1e-16)
                recall75 = torch.sum(iou75 * detected_mask) / (obj_mask.sum() + 1e-16)

                if use_angle == 'True':
                    self.metrics = {
                    "loss": to_cpu(total_loss).item(),
                    "x": to_cpu(loss_x).item(),
                    "y": to_cpu(loss_y).item(),
                    "w": to_cpu(loss_w).item(),
                    "h": to_cpu(loss_h).item(),
                    "angle": to_cpu(loss_a).item(),
                    "conf": to_cpu(loss_conf).item(),
                    "cls": to_cpu(loss_cls).item(),
                    "cls_acc": to_cpu(cls_acc).item(),
                    "recall50": to_cpu(recall50).item(),
                    "recall75": to_cpu(recall75).item(),
                    "precision": to_cpu(precision).item(),
                    "conf_obj": to_cpu(conf_obj).item(),
                    "conf_noobj": to_cpu(conf_noobj).item(),
                    "grid_size": grid_size,
                }

                else:
                    self.metrics = {
                        "loss": to_cpu(total_loss).item(),
                        "x": to_cpu(loss_x).item(),
                        "y": to_cpu(loss_y).item(),
                        "w": to_cpu(loss_w).item(),
                        "h": to_cpu(loss_h).item(),
                        #"angle": to_cpu(loss_a).item(),
                        "conf": to_cpu(loss_conf).item(),
                        "cls": to_cpu(loss_cls).item(),
                        "cls_acc": to_cpu(cls_acc).item(),
                        "recall50": to_cpu(recall50).item(),
                        "recall75": to_cpu(recall75).item(),
                        "precision": to_cpu(precision).item(),
                        "conf_obj": to_cpu(conf_obj).item(),
                        "conf_noobj": to_cpu(conf_noobj).item(),
                        "grid_size": grid_size,
                    }

                return output, total_loss

        elif uda_method == 'minent':
            # #minent 18 
            # pred_prob = torch.cat((pred_cls , 1-pred_cls ), -1)    ### can create probability of not belonging to person class as values are 
            # feat_map = pred_prob  
                                                                    ### normalized between 0-1 using sigmoid

            # ### minent19
            # pred_prob = torch.nn.functional.softmax(prediction[...,6:], dim=4)  
            # feat_map = pred_prob      ### can apply softmax

            # ### minent20
            filter_ind = torch.where( pred_conf > self.ignore_thres )
            # pred_prob = pred_cls[filter_ind[0], filter_ind[1], filter_ind[2], filter_ind[3]] 
            pred_prob = prediction[filter_ind[0], filter_ind[1], filter_ind[2], filter_ind[3],6:] 
            pred_prob = torch.nn.functional.softmax(pred_prob, dim=1)
            feat_map = pred_prob
            
            loss_ent = self.entropy_loss(feat_map)
            total_loss = self.entropy_lambda * loss_ent

            self.uda_metrics = {
                "minent": to_cpu(loss_ent).item(),
            }

            return output, total_loss

class Darknet(nn.Module):
    """YOLOv3 object detection model"""

    def __init__(self, config_path, img_size=416):
        super(Darknet, self).__init__()
        self.module_defs = parse_model_config(config_path)
        self.hyperparams, self.module_list = create_modules(self.module_defs)
        self.yolo_layers = [layer[0] for layer in self.module_list if hasattr(layer[0], "metrics")]
        self.img_size = img_size
        self.seen = 0
        self.header_info = np.array([0, 0, 0, self.seen, 0], dtype=np.int32)

    def forward(self, x, use_angle=False, targets=None, uda_method=None):
        img_dim = x.shape[2]
        loss = 0
        layer_outputs, yolo_outputs = [], []
        for i, (module_def, module) in enumerate(zip(self.module_defs, self.module_list)):
            if module_def["type"] in ["convolutional", "upsample", "maxpool"]:
                x = module(x)
            elif module_def["type"] == "route":
                x = torch.cat([layer_outputs[int(layer_i)] for layer_i in module_def["layers"].split(",")], 1)
            elif module_def["type"] == "shortcut":
                layer_i = int(module_def["from"])
                x = layer_outputs[-1] + layer_outputs[layer_i]
            elif module_def["type"] == "yolo":
                x, layer_loss = module[0](x, targets=targets, img_dim=img_dim, use_angle=use_angle, uda_method=uda_method)
                loss += layer_loss
                yolo_outputs.append(x)
            layer_outputs.append(x)
        yolo_outputs = to_cpu(torch.cat(yolo_outputs, 1))
        if uda_method == None:
            return yolo_outputs if targets is None else (loss, yolo_outputs)
        elif uda_method == 'minent':
            return (loss, yolo_outputs)

    def load_darknet_weights(self, weights_path):
        """Parses and loads the weights stored in 'weights_path'"""

        # Open the weights file
        with open(weights_path, "rb") as f:
            header = np.fromfile(f, dtype=np.int32, count=5)  # First five are header values
            self.header_info = header  # Needed to write header when saving weights
            self.seen = header[3]  # number of images seen during training
            weights = np.fromfile(f, dtype=np.float32)  # The rest are weights

        # Establish cutoff for loading backbone weights
        cutoff = None
        if "darknet53.conv.74" in weights_path:
            cutoff = 75

        ptr = 0
        for i, (module_def, module) in enumerate(zip(self.module_defs, self.module_list)):
            if i == cutoff:
                break
            if module_def["type"] == "convolutional":
                conv_layer = module[0]
                if module_def["batch_normalize"]:
                    # Load BN bias, weights, running mean and running variance
                    bn_layer = module[1]
                    num_b = bn_layer.bias.numel()  # Number of biases
                    # Bias
                    bn_b = torch.from_numpy(weights[ptr : ptr + num_b]).view_as(bn_layer.bias)
                    bn_layer.bias.data.copy_(bn_b)
                    ptr += num_b
                    # Weight
                    bn_w = torch.from_numpy(weights[ptr : ptr + num_b]).view_as(bn_layer.weight)
                    bn_layer.weight.data.copy_(bn_w)
                    ptr += num_b
                    # Running Mean
                    bn_rm = torch.from_numpy(weights[ptr : ptr + num_b]).view_as(bn_layer.running_mean)
                    bn_layer.running_mean.data.copy_(bn_rm)
                    ptr += num_b
                    # Running Var
                    bn_rv = torch.from_numpy(weights[ptr : ptr + num_b]).view_as(bn_layer.running_var)
                    bn_layer.running_var.data.copy_(bn_rv)
                    ptr += num_b
                else:
                    # Load conv. bias
                    num_b = conv_layer.bias.numel()
                    conv_b = torch.from_numpy(weights[ptr : ptr + num_b]).view_as(conv_layer.bias)
                    conv_layer.bias.data.copy_(conv_b)
                    ptr += num_b
                # Load conv. weights
                num_w = conv_layer.weight.numel()
                conv_w = torch.from_numpy(weights[ptr : ptr + num_w]).view_as(conv_layer.weight)
                conv_layer.weight.data.copy_(conv_w)
                ptr += num_w

    def save_darknet_weights(self, path, cutoff=-1):
        """
            @:param path    - path of the new weights file
            @:param cutoff  - save layers between 0 and cutoff (cutoff = -1 -> all are saved)
        """
        fp = open(path, "wb")
        self.header_info[3] = self.seen
        self.header_info.tofile(fp)

        # Iterate through layers
        for i, (module_def, module) in enumerate(zip(self.module_defs[:cutoff], self.module_list[:cutoff])):
            if module_def["type"] == "convolutional":
                conv_layer = module[0]
                # If batch norm, load bn first
                if module_def["batch_normalize"]:
                    bn_layer = module[1]
                    bn_layer.bias.data.cpu().numpy().tofile(fp)
                    bn_layer.weight.data.cpu().numpy().tofile(fp)
                    bn_layer.running_mean.data.cpu().numpy().tofile(fp)
                    bn_layer.running_var.data.cpu().numpy().tofile(fp)
                # Load conv bias
                else:
                    conv_layer.bias.data.cpu().numpy().tofile(fp)
                # Load conv weights
                conv_layer.weight.data.cpu().numpy().tofile(fp)

        fp.close()
