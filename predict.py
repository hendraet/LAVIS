"""
Generates image caption with BLIP (image path specified with --filepath)
Generates attention map for the caption to visualize important regions in the image
- saves results to lavis/output/BLIP/inference/,
  attention map images are saved in a subfolder, named after the original image

Pass Arguments:
    --use_nucleus_sampling sets decoding strategy to nucleus sampling if true,
      otherwise beam search
    --max_length sets maximal token length of generated caption (default 50)
    --forcewords specifies words you wish to include in the caption (optional)
    --num_captions sets the number of captions generated by the BLIP model
    --model_type sets captioner model configuration in lavis/configs/models/
"""
import requests
import torch
import numpy as np
from typing import Optional, List
import csv
from pathlib import Path
import argparse
from PIL import Image, ImageOps
from lavis.models import load_model_and_preprocess
from matplotlib import pyplot as plt, image as pltimg
from lavis.common.gradcam import getAttMap
from lavis.models.blip_models.blip_image_text_matching import compute_gradcam
from pathlib import Path

output_path = Path("lavis/output/BLIP/caption_inference")


def parse_args():
    parser = argparse.ArgumentParser(description="Inference")

    parser.add_argument("--image_path", required=True, type=str)
    parser.add_argument("--max_length", required=False, default=30, type=int)
    parser.add_argument("--num_captions", required=False, default=3, type=int)
    parser.add_argument("--model_type", required=False, default="base_coco", type=str)

    parser.add_argument(
        "--use_nucleus_sampling", required=False, default=True, type=bool
    )
    parser.add_argument(
        "-force_words",
        required=False,
        action="store",
        dest="force_words",
        type=str,
        nargs="*",
        help="usage: -force_words 'words' 'you want to' 'force'",
    )

    args = parser.parse_args()
    return args


def img_name_from(url: str):
    return url.split("/")[-1].split(".")[0] + ".png"


def load_image(img_url: str):
    return Image.open(requests.get(img_url, stream=True).raw).convert("RGB")


def infer_caption(
    raw_image,
    captioner,
    force_words: Optional[List[str]] = None,
    max_length: int = 50,
    use_nucleus_sampling: bool = True,
    num_captions: int = 3,
):
    """
    Generates captions for a sample image
    - Prepare the force words as model input using the associated tokenizer
    - Align parameters for beam Search instead of nucleus sampling
    - Prepare the image as model input using the associated processors
    - Model generates multiple captions if nucleus sampling and one caption if beam search
    """
    model, vis_processors, text_processors = captioner

    force_words_ids = None
    if force_words:
        # Adjust params for beam search
        use_nucleus_sampling = False

        force_words_ids = model.tokenizer(
            force_words, add_special_tokens=False
        ).input_ids

    # Beam search is deterministic, each caption for specific image identical
    if not use_nucleus_sampling:
        num_captions = 1

    image = vis_processors["eval"](raw_image).unsqueeze(0).to(device)
    pred_captions = model.generate(
        {"image": image},
        use_nucleus_sampling=use_nucleus_sampling,
        num_captions=num_captions,
        num_beams=7,
        force_words_ids=force_words_ids,
        max_length=max_length,
    )

    return pred_captions


def adjust_img_att(raw_image, gray_scale: bool = True):
    """
    This method prepares the raw image lying under the attention map
    - Optinally Grayscale image (for better visualization)
    - Adjust image size to preprocessing within BLIP
    - Normalize image values (result: between 0 and 1)
    """
    if gray_scale:
        gray = ImageOps.grayscale(raw_image)
    dst_w = 720
    w, h = raw_image.size
    scaling_factor = dst_w / w

    resized_img = gray.resize((int(w * scaling_factor), int(h * scaling_factor)))
    norm_img = np.float32(resized_img) / 255

    # Adjust array to have 3 color channels
    norm_img = np.stack((norm_img,) * 3, axis=-1)
    return norm_img


def visualize_attention(raw_image, img_text_matcher, caption):
    """
    Preprocess image and text inputs
    Compute  Gradient-weighted Class Activation Mapping (gradcam)
    Average GradCam for the full image
    - Results into attention map on the original image
    """
    model, vis_processors, text_processors = img_text_matcher

    image = vis_processors["eval"](raw_image).unsqueeze(0).to(device)
    norm_img = adjust_img_att(raw_image)

    text = text_processors["eval"](caption)
    text_tokens = model.tokenizer(text, return_tensors="pt").to(device)

    gradcam, _ = compute_gradcam(model, image, text, text_tokens, block_num=7)
    avg_gradcam = getAttMap(norm_img, gradcam[0][1], blur=True)
    # Normalize added value from attention
    avg_gradcam = (avg_gradcam - avg_gradcam.min()) / (
        avg_gradcam.max() - avg_gradcam.min()
    )
    return avg_gradcam


def log_captions(captions: List[str], file_name: str):
    Path(output_path).mkdir(parents=True, exist_ok=True)
    entries = [file_name]
    entries.extend(captions)
    with open(output_path / "output.csv", "a") as csvfile:
        csvwriter = csv.writer(csvfile)
        csvwriter.writerow(entries)


if __name__ == "__main__":
    args = parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # loads BLIP caption base model, with finetuned checkpoints on MSCOCO captioning dataset.
    captioner = load_model_and_preprocess(
        name="blip_caption", model_type=args.model_type, is_eval=True, device=device
    )
    img_text_matcher = load_model_and_preprocess(
        name="blip_image_text_matching", model_type="large", device=device, is_eval=True
    )

    file = Path(args.image_path)
    raw_image = Image.open(file)

    captions = infer_caption(
        raw_image,
        captioner,
        force_words=args.force_words,
        max_length=args.max_length,
        use_nucleus_sampling=args.use_nucleus_sampling,
        num_captions=args.num_captions,
    )
    log_captions(captions, file.name)

    for caption in captions:
        attention_img = visualize_attention(raw_image, img_text_matcher, caption)

        Path(output_path / "images" / file.stem).mkdir(parents=True, exist_ok=True)
        img_name = "_".join(caption.split(" ")) + "_attention.png"
        pltimg.imsave(output_path / "images" / file.stem / img_name, attention_img)
