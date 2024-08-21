import tempfile
import json
import os
from pycocotools.coco import COCO
from pycocotools import mask as maskUtils
import matplotlib.pyplot as plt
from icecream import ic
import cv2
import numpy as np
from copy import deepcopy
import uuid
import os
import torch
import numpy as np
import matplotlib.pyplot as plt
import pickle
from typing import List, Dict, TypedDict, NamedTuple, Generator, Tuple, Set

from PIL import Image
from utils import (
    show_mask,
    show_points,
    show_box,
    mask_to_masks,
    mask_to_bbox,
    mask_to_points,
)
from loguru import logger

# Enable autocast for mixed precision on CUDA devices
torch.autocast(device_type="cuda", dtype=torch.bfloat16).__enter__()

# Enable TensorFloat32 (tf32) for Ampere GPUs
if torch.cuda.get_device_properties(0).major >= 8:
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

from sam2.build_sam import build_sam2_video_predictor

# Path to the SAM2 checkpoint and model configuration
sam2_checkpoint = (
    "/bd_byta6000i0/users/sam2/kyyang/SurgicalSAM2/checkpoints/sam2_hiera_tiny.pt"
)
model_cfg = "sam2_hiera_t.yaml"

######################
#
# torch initialize
#
######################


PROMPT_INFO = []
OUTPUT_PATH = None
VIDEO_ID_SET = set()
COCO_INFO = None
OBJ_COUNT = 0

########################
#
# initialize the global variables
#
########################


class PromptInfo(TypedDict):
    """Typed dictionary for storing prompt information."""

    prompt_objs: List[Dict]
    frame_idx: int
    prompt_type: str
    video_id: str
    path: str


class ClipRange(NamedTuple):
    """Named tuple for storing clip range."""

    start_idx: int
    end_idx: int


################################################################################
#
# type definitions
#
################################################################################
def get_dicts_by_field_value(data, field_name, target_value):
    """
    Get dictionaries from a list where a specified field has a specific value.

    Args:
        data (list): List of dictionaries.
        field_name (str): Field name to check.
        target_value: Value to match.

    Returns:
        list: List of dictionaries that match the criteria.
    """
    return [item for item in data if item.get(field_name) == target_value]


def sort_dicts_by_field(data, field_name, reverse=False):
    """
    Sort a list of dictionaries by a specified field.

    Args:
        data (list): List of dictionaries.
        field_name (str): Field name to sort by.
        reverse (bool): Whether to sort in descending order.

    Returns:
        list: Sorted list of dictionaries.
    """
    return sorted(data, key=lambda item: item.get(field_name), reverse=reverse)


def get_imgs(coco_info):
    """
    Get images from COCO dataset.

    Args:
        coco_info (COCO): COCO dataset object.

    Returns:
        list: List of image information.
    """
    img_ids = coco_info.getImgIds()
    imgs = coco_info.loadImgs(img_ids)
    return imgs


################################################################################
#
# helper functions
#
################################################################################


def find_prompt_frame(frames, clip_range: ClipRange):
    """
    Find the first frame within the clip range that has annotations.

    Args:
        frames (list): List of frame information.
        clip_range (ClipRange): Range of the clip.

    Returns:
        dict: Information about the prompt frame.
    """
    clip_start = clip_range.start_idx
    clip_end = clip_range.end_idx

    for frame in frames:
        if frame["is_det_keyframe"] == False:
            continue

        if frame["order_in_video"] < clip_start or frame["order_in_video"] > clip_end:
            continue

        ann_ids = COCO_INFO.getAnnIds(imgIds=frame["id"])

        if ann_ids == []:
            continue

        return frame

    return None


def create_symbol_link_for_video(frames_info):
    """
    Create symbolic links for video frames in a temporary directory.

    Args:
        frames_info (list): List of frame information.

    Returns:
        str: Path to the temporary directory.
    """
    video_dir = tempfile.mkdtemp()

    for idx, frame in enumerate(frames_info):
        frame_name = str(idx).zfill(8)  # 填充到5位宽度
        dst_path = os.path.join(video_dir, f"{frame_name}.jpg")
        src_path = frame["path"]
        os.symlink(src_path, dst_path)

    return video_dir


def get_each_obj(prompt_frame, num_points=1, cats: Set[int] = None):
    """
    Extract objects from the prompt frame.

    Args:
        prompt_frame (dict): Information about the prompt frame.
        num_points (int): Number of points to extract.
        cats (Set[int]): Set of category IDs to filter by.

    Returns:
        list: List of objects.
    """
    global OBJ_COUNT
    ann_ids = COCO_INFO.getAnnIds(imgIds=prompt_frame["id"])
    anns = COCO_INFO.loadAnns(ann_ids)
    num_cat = len(COCO_INFO.cats)

    objs = []
    for ann in anns:
        if cats is not None and ann["category_id"] not in cats:
            continue
        rle = ann["segmentation"]
        mask = maskUtils.decode(rle)  # 将RLE解码为二进制掩码
        masks = mask_to_masks(mask)
        for mask in masks:
            obj = {
                "mask": mask,
                "bbox": mask_to_bbox(mask),
                "points": mask_to_points(mask, num_points),
                "obj_id": OBJ_COUNT * (num_cat + 1) + ann["category_id"],
            }
            objs.append(obj)
            OBJ_COUNT += 1

    return objs


def get_obj_from_masks(video_segment):
    """
    Extract objects from video segments.

    Args:
        video_segment (dict): Dictionary containing video segments.

    Returns:
        list: List of objects.
    """
    objs = []
    for obj_id, obj_seg in video_segment.items():
        if obj_seg["mask"].sum() == 0:
            continue

        masks = mask_to_masks(np.squeeze(obj_seg["mask"], axis=0))
        for mask in masks:
            obj = {
                "mask": mask,
                "bbox": mask_to_bbox(mask),
                "points": mask_to_points(mask),
                "obj_id": obj_id,
            }
            objs.append(obj)
    return objs


def add_prompt(
    prompt_objs,
    predictor,
    inference_state,
    prompt_frame_order_in_video,
    prompt_type,
):
    """
    Add prompts to the predictor.

    Args:
        prompt_objs (list): List of prompt objects.
        predictor (object): Predictor object.
        inference_state (object): Inference state object.
        prompt_frame_order_in_video (int): Order of the prompt frame in the video.
        prompt_type (str): Type of prompt (points, bbox, mask).

    Returns:
        tuple: Updated predictor, inference state, object IDs, and mask logits.
    """
    for obj in prompt_objs:
        match prompt_type:
            case "points":
                _, out_obj_ids, out_mask_logits = predictor.add_new_points_or_box(
                    inference_state=inference_state,
                    frame_idx=prompt_frame_order_in_video,
                    obj_id=obj["obj_id"],
                    points=obj["points"],
                    labels=np.ones(len(obj["points"])),
                )
            case "bbox":
                _, out_obj_ids, out_mask_logits = predictor.add_new_points_or_box(
                    inference_state=inference_state,
                    frame_idx=prompt_frame_order_in_video,
                    obj_id=obj["obj_id"],
                    box=obj["bbox"],
                )
            case "mask":
                mask_tensor = torch.from_numpy(obj["mask"]).to(torch.bool)
                _, out_obj_ids, out_mask_logits = predictor.add_new_mask(
                    inference_state=inference_state,
                    frame_idx=prompt_frame_order_in_video,
                    obj_id=obj["obj_id"],
                    mask=mask_tensor,
                )

    return predictor, inference_state, out_obj_ids, out_mask_logits


def predict_on_video(predictor, inference_state, start_idx):
    """
    Predict segmentation masks for the video.

    Args:
        predictor (object): Predictor object.
        inference_state (object): Inference state object.
        start_idx (int): Start index of the video.

    Returns:
        dict: Dictionary containing video segments.
    """
    video_segments = {}
    # video_segments contains the per-frame segmentation results
    for out_frame_idx, out_obj_ids, out_mask_logits in predictor.propagate_in_video(
        inference_state, reverse=True
    ):
        video_segments[out_frame_idx + start_idx] = {
            out_obj_id: {
                "mask": (out_mask_logits[i] > 0.0).cpu().numpy(),
                "score": torch.sigmoid(out_mask_logits[i])
                .max()
                .item(),  # 使用 sigmoid 转换为概率，取最大值作为 score
            }
            for i, out_obj_id in enumerate(out_obj_ids)
        }
    for out_frame_idx, out_obj_ids, out_mask_logits in predictor.propagate_in_video(
        inference_state
    ):
        video_segments[out_frame_idx + start_idx] = {
            out_obj_id: {
                "mask": (out_mask_logits[i] > 0.0).cpu().numpy(),
                "score": torch.sigmoid(out_mask_logits[i])
                .max()
                .item(),  # 使用 sigmoid 转换为概率，取最大值作为 score
            }
            for i, out_obj_id in enumerate(out_obj_ids)
        }
    return video_segments


def save_prompt_frame(
    clip_prompts: List[PromptInfo],
):
    """
    Save prompt frames.

    Args:
        clip_prompts (List[PromptInfo]): List of prompt information.
    """
    global PROMPT_INFO
    PROMPT_INFO.extend(clip_prompts)


def process_video_clip(frames, clip_prompts: List[PromptInfo], clip_range: ClipRange):
    """
    Process a video clip.

    Args:
        frames (list): List of frame information.
        clip_prompts (List[PromptInfo]): List of prompt information.
        clip_range (ClipRange): Range of the clip.

    Returns:
        dict: Dictionary containing video segments.
    """
    start_idx = clip_range.start_idx
    end_idx = clip_range.end_idx

    video_dir = create_symbol_link_for_video(frames[start_idx : end_idx + 1])

    predictor = build_sam2_video_predictor(model_cfg, sam2_checkpoint)
    inference_state = predictor.init_state(video_path=video_dir)

    for prompt_info in clip_prompts:

        prompt_objs = prompt_info["prompt_objs"]
        prompt_frame_idx = prompt_info["frame_idx"] - start_idx
        prompt_type = prompt_info["prompt_type"]

        predictor, inference_state, out_obj_ids, out_mask_logits = add_prompt(
            prompt_objs,
            predictor,
            inference_state,
            prompt_frame_idx,
            prompt_type,
        )

    video_segments = predict_on_video(predictor, inference_state, start_idx)

    del predictor
    torch.cuda.empty_cache()

    return video_segments


def get_clip_prompts(frames, prompt_type, clip_length: int = None):
    """
    Generate clip prompts.

    Args:
        frames (list): List of frame information.
        prompt_type (str): Type of prompt (points, bbox, mask).
        clip_length (int): Length of the clip.

    Yields:
        tuple: List of prompt information and clip range.
    """
    if clip_length is None:
        clip_length = len(frames)

    for start_idx in range(0, len(frames), clip_length):

        end_idx = min(start_idx + clip_length - 1, len(frames) - 1)

        clip_range = ClipRange(start_idx, end_idx)

        prompt_frame = find_prompt_frame(frames, clip_range)

        if prompt_frame is None:
            logger.warning(
                f"No prompt frame found for clip {clip_range} for video {frames[0]['video_id']}"
            )
            continue

        prompt_objs = get_each_obj(prompt_frame)

        yield [
            PromptInfo(
                prompt_objs=prompt_objs,
                frame_idx=prompt_frame["order_in_video"],
                prompt_type=prompt_type,
                video_id=str(prompt_frame["video_id"]),
                path=prompt_frame["path"],
            )
        ], clip_range


def get_num_categories(frame):
    """
    Get the number of categories in a frame.

    Args:
        frame (dict): Information about the frame.

    Returns:
        set: Set of category IDs.
    """
    ann_ids = COCO_INFO.getAnnIds(imgIds=frame["id"])
    anns = COCO_INFO.loadAnns(ann_ids)
    cat_set = set()
    for ann in anns:
        cat_set.add(ann["category_id"])
    return cat_set


def get_prompts_from_categories(frames):
    """
    Generate prompts based on categories.

    Args:
        frames (list): List of frame information.

    Returns:
        list: List of prompt information and clip ranges.
    """
    existing_cats = set()
    previous_start_idx = None
    previous_prompt_info = None
    prompt_and_ranges = []

    for frame in frames:
        if frame["is_det_keyframe"] == False:
            continue
        if get_num_categories(frame).issubset(existing_cats):
            continue

        diff_cats = get_num_categories(frame).difference(existing_cats)
        existing_cats = existing_cats.union(diff_cats)

        prompt_objs = get_each_obj(frame)

        if prompt_objs is None:
            continue

        prompt_frame_idx = frame["order_in_video"]
        prompt_type = "points"

        prompt_info = PromptInfo(
            prompt_objs=prompt_objs,
            frame_idx=prompt_frame_idx,
            prompt_type=prompt_type,
            video_id=str(frame["video_id"]),
            path=frame["path"],
        )

        if previous_prompt_info is None:
            previous_prompt_info = prompt_info
            previous_start_idx = prompt_frame_idx
            continue

        prompt_and_ranges.append(
            [previous_prompt_info],
            ClipRange(previous_start_idx, prompt_frame_idx),
        )
        previous_prompt_info = prompt_info
        previous_start_idx = prompt_frame_idx

    if previous_start_idx != len(frames) - 1:
        prompt_and_ranges.append(
            ([previous_prompt_info], ClipRange(previous_start_idx, len(frames) - 1))
        )

    return prompt_and_ranges


def process_singel_video(
    frames, prompt_type, clip_length: int = None, variable_cats: bool = False
):
    """
    Process a single video.

    Args:
        frames (list): List of frame information.
        prompt_type (str): Type of prompt (points, bbox, mask).
        clip_length (int): Length of the clip.
        variable_cats (bool): Whether to use variable categories for prompts.

    Returns:
        dict: Dictionary containing video segments.
    """
    global OBJ_COUNT
    OBJ_COUNT = 0

    video_segments = {}

    previous_frame_mask_prompt: PromptInfo = None

    if variable_cats:
        gen_clip_prompts = get_prompts_from_categories(frames)
    else:
        gen_clip_prompts = get_clip_prompts(frames, prompt_type, clip_length)

    for clip_prompts, clip_range in gen_clip_prompts:

        if variable_cats and previous_frame_mask_prompt is not None:
            clip_prompts.append(previous_frame_mask_prompt)

        save_prompt_frame(clip_prompts)
        logger.info(clip_range)
        video_segments.update(process_video_clip(frames, clip_prompts, clip_range))

        if variable_cats:
            previous_frame_mask_prompt = PromptInfo(
                prompt_objs=get_obj_from_masks(video_segments[clip_range.end_idx]),
                frame_idx=clip_range.end_idx,
                prompt_type=prompt_type,
                video_id=str(frames[0]["video_id"]),
                path=frames[clip_range.end_idx]["path"],
            )

    torch.cuda.empty_cache()
    return video_segments


def process_all_videos(prompt_type, clip_length, variable_cats):
    """
    Process all videos.

    Args:
        prompt_type (str): Type of prompt (points, bbox, mask).
        clip_length (int): Length of the clip.

    Returns:
        dict: Dictionary containing all video segments.
    """
    all_video_segments = {}
    for video_id in VIDEO_ID_SET:
        logger.info(f"video_id: {video_id}")
        frames = get_dicts_by_field_value(get_imgs(COCO_INFO), "video_id", video_id)
        video_segments = process_singel_video(
            frames, prompt_type, clip_length, variable_cats
        )
        all_video_segments[video_id] = video_segments
        torch.cuda.empty_cache()
        free_memory, total_memory = torch.cuda.mem_get_info()
        logger.info(f"free memory: {free_memory/1024**3:.2f} GB")

    return all_video_segments


def save_as_coco_format(all_video_segments, save_video_list):
    """
    Save the results in COCO format.

    Args:
        all_video_segments (dict): Dictionary containing all video segments.
        save_video_list (list): The videos to save.

    Returns:
        tuple: Paths to the saved prediction and prompt files.
    """
    coco_annotations: list = []
    num_cat: int = len(COCO_INFO.cats)

    if save_video_list is None:
        save_video_list = VIDEO_ID_SET

    for video_id in save_video_list:
        video_segments = all_video_segments[video_id]

        frames = get_dicts_by_field_value(get_imgs(COCO_INFO), "video_id", video_id)
        frames = sort_dicts_by_field(frames, "order_in_video")

        for frame in frames:
            if frame["is_det_keyframe"] == False:
                continue

            merged_mask = {}

            ## merge the mask
            for key, mask_info in video_segments[frame["order_in_video"]].items():
                remainder = key % (num_cat + 1)
                m_mask = np.logical_or.reduce(
                    mask_info["mask"], axis=0
                )  # 使用 mask_info['mask']
                score = mask_info["score"]  # 获取 score

                if remainder not in merged_mask:
                    merged_mask[remainder] = m_mask
                else:
                    merged_mask[remainder] = np.logical_or(
                        merged_mask[remainder], m_mask
                    )

            for key, mask in merged_mask.items():
                if mask.sum() == 0:
                    continue

                rle = maskUtils.encode(np.asfortranarray(mask))
                rle["counts"] = rle["counts"].decode("utf-8")
                annotation = {
                    "image_id": frame["id"],
                    "category_id": key,
                    "segmentation": rle,
                    "bbox": mask_to_bbox(mask),  # 添加 bbox 字段
                    "iscrowd": 0,
                    "score": score,  # 添加 score 字段
                }
                coco_annotations.append(annotation)

    predict_data = coco_annotations

    predict_path = os.path.join(OUTPUT_PATH, "predict.json")
    prompt_path = os.path.join(OUTPUT_PATH, "prompt.pkl")

    with open(predict_path, "w") as f:
        json.dump(predict_data, f, indent=4)

    with open(prompt_path, "wb") as f:
        pickle.dump(PROMPT_INFO, f)

    return predict_path, prompt_path


def inference(
    coco_path, output_path, prompt_type, clip_length, variable_cats, save_video_list
):
    """
    Perform inference on COCO dataset.

    Args:
        coco_path (str): Path to COCO annotations file.
        output_path (str): Path to output directory.
        prompt_type (str): Type of prompt (points, bbox, mask).
        clip_length (int): Length of the clip.
        save_video_list (list): The videos to save.

    Returns:
        tuple: Paths to the saved prediction and prompt files.
    """
    global OUTPUT_PATH, VIDEO_ID_SET, COCO_INFO

    OUTPUT_PATH = os.path.join(output_path, "output", prompt_type)
    os.makedirs(OUTPUT_PATH, exist_ok=True)

    COCO_INFO = COCO(coco_path)

    img_ids = COCO_INFO.getImgIds()
    imgs = COCO_INFO.loadImgs(img_ids)

    for img in imgs:
        VIDEO_ID_SET.add(img["video_id"])

    all_videos_segments = process_all_videos(prompt_type, clip_length, variable_cats)

    predict_path, prompt_path = save_as_coco_format(
        all_videos_segments, save_video_list
    )

    return predict_path, prompt_path


if __name__ == "__main__":
    # global OUTPUT_PATH

    inference(
        coco_path="coco_annotations.json",
        output_path="./test",
        prompt_type="points",
        clip_length=None,
        variable_cats=False,
        save_video_list=None,
    )
