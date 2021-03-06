import torch
import torch.nn.functional as F
import numpy as np
import glob
from PIL import Image

import imgaug.augmenters as iaa
from imgaug.augmentables.bbs import BoundingBox, BoundingBoxesOnImage
from imgaug.augmentables.segmaps import SegmentationMapsOnImage

import torchvision.transforms as transforms
from .utils import xywh2xyxy_np

from utils.fda import FDA_source_to_target_np


class ImgAug(object):
    def __init__(self, augmentations=[]):
        self.augmentations = augmentations

    def __call__(self, data):
        # Unpack data
        img, boxes = data

        # Convert xywh to xyxy
        boxes = np.array(boxes[:,:-1])
        boxes[:, 1:] = xywh2xyxy_np(boxes[:, 1:])
        
        # Convert bounding boxes to imgaug        
        bounding_boxes = BoundingBoxesOnImage(
            [BoundingBox(*box[1:], label=box[0]) for box in boxes], 
            shape=img.shape)

        # Apply augmentations
        img, bounding_boxes = self.augmentations(
            image=img, 
            bounding_boxes=bounding_boxes)

        # Clip out of image boxes
        bounding_boxes = bounding_boxes.clip_out_of_image()

        # Convert bounding boxes back to numpy
        boxes = np.zeros((len(bounding_boxes), 6))
        for box_idx, box in enumerate(bounding_boxes):
            # Extract coordinates for unpadded + unscaled image
            x1 = box.x1
            y1 = box.y1
            x2 = box.x2
            y2 = box.y2

            # Returns (x, y, w, h)
            boxes[box_idx, 0] = box.label
            boxes[box_idx, 1] = ((x1 + x2) / 2)
            boxes[box_idx, 2] = ((y1 + y2) / 2)
            boxes[box_idx, 3] = (x2 - x1)
            boxes[box_idx, 4] = (y2 - y1)

        return img, boxes

class AbsoluteLabels(object):
    def __init__(self, ):
        pass

    def __call__(self, data):
        img, boxes = data
        w, h, _ = img.shape 
        boxes[:,[1,3]] *= h
        boxes[:,[2,4]] *= w
        return img, boxes

class PadSquare(ImgAug):
    def __init__(self, ):
        self.augmentations = iaa.Sequential([
            iaa.PadToAspectRatio(
                1.0,
                position="center-center").to_deterministic()
            ])

class RelativeLabels(object):
    def __init__(self, ):
        pass

    def __call__(self, data):
        img, boxes = data
        w, h, _ = img.shape 
        boxes[:,[1,3]] /= h
        boxes[:,[2,4]] /= w
        return img, boxes

class ToTensor(object):
    def __init__(self, ):
        pass

    def __call__(self, data):
        img, boxes = data
        # Extract image as PyTorch tensor
        img = transforms.ToTensor()(img)

        bb_targets = torch.zeros((len(boxes), 7))
        bb_targets[:, 1:] = transforms.ToTensor()(boxes)

        return img, bb_targets

class jitter(object):
    def __init__(self, ):
        pass

    def __call__(self, data):
        img, boxes = data

        img = transforms.ColorJitter(0.3, 0.3, 0.3, 0.3)(img)
        img = np.array(img, dtype=np.uint8)

        return img, boxes

class Resize(object):
    def __init__(self, size):
        self.size = size

    def __call__(self, data):
        img, boxes = data
        img = F.interpolate(img.unsqueeze(0), size=self.size, mode="nearest").squeeze(0)
        return img, boxes

class fda_adapt(object):
    def __init__(self, beta, mask ):
        self.trg_files = glob.glob('/localdata/saurabh/yolov3/data/cepdof/all_images/*')
        self.beta = beta
        self.circle_mask = mask

    def __call__(self,data):
        img, boxes = data

        trg_path = self.trg_files[ np.random.randint(len(self.trg_files)) ]
        trg_img = Image.open(trg_path).convert('RGB')
        trg_img = trg_img.resize(img.shape[:2], resample=Image.BILINEAR)

        img = FDA_source_to_target_np(img, trg_img, L=self.beta, use_circular=self.circle_mask)

        return img, boxes

class basic_aug(object):
    def __init__(self, ):
        pass

    def __call__(self,data):
        img, boxes = data

        images = torch.flip(img, [-1])
        boxes[:, 2] = 1 - boxes[:, 2]
        return images, boxes
