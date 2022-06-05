#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@author : Romain Graux
@date : 2022 May 13, 15:47:09
@last modified : 2022 June 04, 21:55:32
"""

import os
from glob import glob
from tqdm import tqdm
from functools import partial

import hydra
from omegaconf import OmegaConf
from helpers import omegaconf2namespace, Namespace

import numpy as np
import pandas as pd
import tensorflow as tf
from pyntcloud import PyntCloud
from multiprocessing import Pool
from jpegdna.codecs import JpegDNA
import matplotlib.pyplot as plt

from main import CompressionModel, BatchSingleChannelJpegDNA
from src.pc_io import load_pc, get_shape_data, write_df, pa_to_df
from src.processing import pc_to_occupancy_grid
from utils import pc_dir_to_ds, extract_path, extract_ext, extract_name


def align_files(*directories):
    """
    Allign all files based on the name of each file and zip them.
    """
    assert all(len(directory) > 0 for directory in directories), "Need at least 1 element in each directory"

    paths = [extract_path(directory[0]) for directory in directories]
    extentions = [extract_ext(directory[0]) for directory in directories] # Assume same extension for all files in a directory
    names = [set([extract_name(fname) for fname in directory]) for directory in directories]
    common_names = set.intersection(*names)

    return [[f'{path}/{name}.{ext}' for name in common_names] for path, ext in zip(paths, extentions)]

def load_file(fname):
    """
    Load a file and return the good format.
    """
    ext = extract_ext(fname)
    if ext == 'npy':
        return np.load(fname)
    elif ext == 'pkl':
        return pickle.load(open(fname, 'rb'))
    elif ext == 'ply':
        return PyntCloud.from_file(fname).points
    else:
        raise Exception(f'Unknown extension {ext}')

def lazy_loader(files, args):
    """
    Load files in a lazy fashion.
    """
    for slice_files in files:
        yield np.squeeze([load_file(fname) for fname in slice_files])

def loader(files, args):
    """
    Load files in cache
    """
    return [load_file(fname) for fname in files]

def load_io_files(args, exception=[]):
    """
    Load all files contained in the io subflag.
    """
    raw_files = [glob(f"{directory}/*") for name, directory in args.io.items() if name not in exception]
    files = align_files(*raw_files)
    return lazy_loader(zip(*files) if len(files)>1 else np.reshape(files, (-1, 1)), args)

# All tasks

def play(args):
    """
    Do not do anything, just play.
    """
    pass

def bypass_dna(args):
    """
    Bypass the compression into oligos and directly reconstruct the point clouds
    """
    ds = pc_dir_to_ds(
            args.io.x,
            args.blocks.resolution,
            args.blocks.channel_last,
            )

    model = CompressionModel(args.architecture)

    os.makedirs("bypass_dna/x_hat", exist_ok=True)

    for data in tqdm(ds, total=ds.cardinality().numpy()):
        x = data["input"]
        name = data["fname"].numpy().decode("UTF-8").split("/")[-1].split(".")[0]

        y = model.analysis_transform(tf.expand_dims(x, 0))
        x_hat = model.synthesis_transform(y)[0]

        pa = np.argwhere(x_hat.numpy() > 0.5).astype("float32")
        write_df(f"./bypass_dna/x_hat/{name}.ply", pa_to_df(pa))

def quantization_tables(args):
    """
    See the impact of the quantization tables on the compression
    """
    from jpegdna.codecs import JPEGDNAGray

    def encode_decode_mse(x, gammas=None, gammas_chroma=None, apply_dct=True):
        if gammas is not None:
            JPEGDNAGray.GAMMAS = gammas
        if gammas_chroma is not None:
            JPEGDNAGray.GAMMAS_CHROMA = gammas_chroma
        codec = JpegDNA(1)
        oligos = codec.encode(x, "from_img", apply_dct=apply_dct)
        reconstructed = codec.decode(oligos, apply_dct=apply_dct)
        return np.mean(np.power(x - reconstructed, 2)), len(np.reshape(oligos, (-1,)))

    # inp = np.random.randint(0, 255, size=(64, 64))
    inp = np.round(np.random.rand(64, 64) * 255)
    ones_block = np.ones((8, 8))

    print(f'MSE with default parameters and dct: {encode_decode_mse(inp)}')
    print(f'MSE with default parameters and no dct: {encode_decode_mse(inp, apply_dct=False)}')

    print(f'MSE with ones and dct: {encode_decode_mse(inp, gammas=ones_block, gammas_chroma=ones_block)}')

def evaluate_y_reconstruction(args):
    """
    Evaluate the reconstruction of y
    """
    global files, y, y_hat
    files = load_io_files(args, exception=['x', 'x_hat'])
    y, y_hat = list(zip(*files))

    print(f"MSE: {np.mean(np.power(np.array(y) - np.array(y_hat), 2))}")
    print(f"Max: {np.max(y)}")
    print(f"Min: {np.min(y)}")





def quantization(args):
    """
    Quantize the float latent representation into quint8
    """
    ds = pc_dir_to_ds(args.io.x, args.blocks.resolution, args.blocks.channel_last)
    x_ds = ds.map(lambda e: tf.expand_dims(e["input"], 0))

    model = CompressionModel(args.architecture)
    for x in tqdm(x_ds, total=x_ds.cardinality().numpy()):
        y = model.analysis_transform(x)


        # Quantize the latent representation
        quantize_range = np.min(y.numpy()), np.max(y.numpy())
        yq, *_ = tf.quantization.quantize(
                y, *quantize_range, tf.quint8
        )


def study_output_analysis(args):
    """
    Study the output of the analysis transform
    """
    global files, x, y, y_hat, x_hat

    files = load_io_files(args)
    for x, y, y_hat, x_hat in files:
        x.plot(kind='hist', bins=64, color='blue', subplots=True)
        x_hat.plot(kind='hist', bins=64, color='blue', subplots=True)
        plt.show()





@hydra.main(config_path="config/benchmarks", config_name='config.yaml', version_base="1.2")
def main(cfg: OmegaConf) -> None:
    global args
    args = omegaconf2namespace(cfg)

    tasks = ["bypass_dna", "quantization_tables", "quantization", "evaluate_y_reconstruction", "play", "study_output_analysis"]
    if args.task not in tasks:
        raise ValueError(f"Task {args.task} not supported, please choose between {tasks}")

    globals()[args.task](args)



if __name__ == '__main__':
    main()
