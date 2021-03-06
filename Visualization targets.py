from __future__ import division

from models import *
from utils.utils import *
from utils.datasets import *

import os
import sys
import time
import datetime
import argparse

from PIL import Image

import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable

import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.ticker import NullLocator

#from train_light import MyModel

import cv2
    
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image_folder", type=str, default="data/fes/single_paths.txt", help="path to dataset")    
    parser.add_argument("--class_path", type=str, default="data/class.names", help="path to class label file")
    parser.add_argument("--batch_size", type=int, default=1, help="size of the batches")
    parser.add_argument("--n_cpu", type=int, default=0, help="number of cpu threads to use during batch generation")
    parser.add_argument("--img_size", type=int, default=416, help="size of each image dimension")
    parser.add_argument("--checkpoint_model", type=str, help="path to checkpoint model")
    parser.add_argument("--use_angle", default='False', help="path to checkpoint model")
    opt = parser.parse_args()
    print(opt)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    os.makedirs("output", exist_ok=True)

    classes = load_classes(opt.class_path)  # Extracts class labels from file

    dataset = ListDataset(opt.image_folder, img_size=opt.img_size, normalized_labels=False, augment=False, 
                multiscale=False, pixel_norm=False, use_angle=opt.use_angle, class_num=len(classes))
    dataloader = DataLoader(
        dataset,
        batch_size=opt.batch_size,
        shuffle=True,
        num_workers=opt.n_cpu,
        collate_fn=dataset.collate_fn
    )

    

    Tensor = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

    imgs = []  # Stores image paths
    img_detections = []  # Stores detections for each image index
    img_detected = 0    ### keep count of detected images 

    print("\nPerforming object detection:")
    prev_time = time.time()
    for batch_i, (img_paths, _ , targets) in enumerate(dataloader):
        
        if targets == None:
            continue

        targets[..., 2:6] = xywh2xyxy(targets[..., 2:6])
        targets[..., 2:6] *= opt.img_size

        annotations = [targets[...,1:]]

        # Log progress
        current_time = time.time()
        inference_time = datetime.timedelta(seconds=current_time - prev_time)
        prev_time = current_time
        print("\t+ Batch %d, Inference Time: %s" % (batch_i, inference_time))

        # Save image and detections
        imgs.extend(img_paths)
        img_detections.extend(annotations)
        img_detected += 1

        if img_detected == 10:
            break

    # Bounding-box colors
    # cmap = plt.get_cmap("tab20b")
    # colors = [cmap(i) for i in np.linspace(0, 1, 20)]
    # colors = colors[:,:3] * 255

    colors = [(0,134,213), (220,0,213), (255,0,0), (255, 233, 0), (0,255,0), (0,0,255)]

    print("\nSaving images:")
    # Iterate through images and save plot of detections
    for img_i, (path, detections) in enumerate(zip(imgs, img_detections)):

        print("(%d) Image: '%s'" % (img_i, path))

        # Create plot
        #img = np.array(Image.open(path))
        img = cv2.imread(path, 1)
        plt.figure()
        fig, ax = plt.subplots(1)
        #ax.imshow(img)

        # Draw bounding boxes and labels of detections
        if detections is not None:
            # Rescale boxes to original image
            detections[...,1:] = rescale_boxes(detections[..., 1:], opt.img_size, img.shape[:2])
            unique_labels = detections[:, 0].cpu().unique()
            n_cls_preds = len(unique_labels)
            bbox_colors = colors

            for cls_pred, x1, y1, x2, y2, angle in detections:

                # New co-ordinates of the rotated bbox
                xy = rotate_detections(x1, y1, x2, y2, angle)
                #Make the co-ordinates compatible for cv2
                pts = np.array(xy, np.int32).reshape((-1,1,2))
                
                # pts = [[x1,y2], [x2,y2], [x2,y1], [x1,y1]]
                # pts = np.array(pts, np.int32).reshape((-1,1,2))

                # box_w = x2 - x1
                # box_h = y2 - y1

                # color = bbox_colors[int(np.where(unique_labels == int(cls_pred))[0])]
                color = bbox_colors[int(cls_pred)]

                #Draw bounding boxes
                cv2.polylines(img, [pts], isClosed=True, color=color, thickness=5)
                cv2.putText(img, classes[int(cls_pred)], (int(x1), int(y1) - 10), cv2.FONT_HERSHEY_SIMPLEX ,  2, color, cv2.LINE_AA)
                ax.imshow(img[...,::-1]) #convert BGR image to RGB image

                # Create a Rectangle patch
                #bbox = patches.Polygon(xy, closed=True, linewidth=2, edgecolor=color, facecolor="none")
                # Add the bbox to the plot
                #ax.add_patch(bbox)

                #Add label
                # plt.text(
                #     x1,
                #     y1,
                #     s=classes[int(cls_pred)],
                #     color="white",
                #     verticalalignment="top",
                #     bbox={"color": color, "pad": 0},
                # )
                

        #if detections is not None:
        # Save generated image with detections
        plt.axis("off")
        plt.gca().xaxis.set_major_locator(NullLocator())
        plt.gca().yaxis.set_major_locator(NullLocator())
        filename = path.split("/")[-1].split(".")[0]
        # plt.savefig(f"output/targets/rotated/{filename}.png", bbox_inches="tight", pad_inches=0.0)
        plt.savefig(f"output/detection/{filename}.png", bbox_inches="tight", pad_inches=0.0)
        plt.close()
