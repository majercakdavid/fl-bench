import json
import os
import pickle
import random
from argparse import ArgumentParser
from collections import Counter

import torch
import numpy as np
from pathlib import Path

from datasets import DATASETS
from partition import dirichlet, iid_partition, randomly_assign_classes, allocate_shards
from util import prune_args, generate_synthetic_data, process_celeba, process_femnist

_CURRENT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


def main(args):
    dataset_root = _CURRENT_DIR.parent / args.dataset

    np.random.seed(args.seed)
    random.seed(args.seed)
    torch.manual_seed(args.seed)

    if not os.path.isdir(dataset_root):
        os.mkdir(dataset_root)

    if args.pretrain_fraction > 0:
        assert args.pretrain_fraction < 1 and int(args.pretrain_fraction * 100) == args.pretrain_fraction * 100, "pretrain_fraction should be a float number between 0 and 1 with at most 2 decimal places."
        assert int(args.client_num_in_total/round(1-args.pretrain_fraction, 2)) == args.client_num_in_total/round(1-args.pretrain_fraction, 2), "client_num_in_total should be divisible by 1-pretrain_fraction without remainder."
        args.client_num_in_total = int(args.client_num_in_total/round(1-args.pretrain_fraction, 2))

    partition = {"separation": None, "data_indices": None}

    print(f"Dataset: {args.dataset}")
    if args.dataset == "femnist":
        partition, stats = process_femnist(args)
    elif args.dataset == "celeba":
        partition, stats = process_celeba(args)
    elif args.dataset == "synthetic":
        partition, stats = generate_synthetic_data(args)
    else:  # MEDMNIST, COVID, MNIST, CIFAR10, ...
        print(f"Downloading {args.dataset} dataset to {dataset_root}, with args: {args}")
        ori_dataset = DATASETS[args.dataset](dataset_root, args)
        print("Download done.")

        if not args.iid:
            if args.alpha > 0:  # Dirichlet(alpha)
                partition, stats = dirichlet(
                    ori_dataset=ori_dataset,
                    num_clients=args.client_num_in_total,
                    alpha=args.alpha,
                    least_samples=args.least_samples,
                )
            elif args.classes != 0:  # randomly assign classes
                args.classes = max(1, min(args.classes, len(ori_dataset.classes)))
                partition, stats = randomly_assign_classes(
                    ori_dataset=ori_dataset,
                    num_clients=args.client_num_in_total,
                    num_classes=args.classes,
                )
            elif args.shards > 0:  # allocate shards
                partition, stats = allocate_shards(
                    ori_dataset=ori_dataset,
                    num_clients=args.client_num_in_total,
                    num_shards=args.shards,
                )
            else:
                raise RuntimeError(
                    "Please set arbitrary one arg from [--alpha, --classes, --shards] to split the dataset."
                )

        else:  # iid partition
            partition, stats = iid_partition(
                ori_dataset=ori_dataset, num_clients=args.client_num_in_total
            )
    print("Partitioning done.")

    if args.pretrain_fraction > 0:
        pretrain_stats = {"x": 0, "y": Counter()}
        for i in range(int(args.pretrain_fraction*args.client_num_in_total), len(partition["data_indices"])):
            pretrain_stats["x"] += stats[i]["x"]
            pretrain_stats["y"].update(stats[i]["y"])

            del stats[i]

        stats["pretrain"] = pretrain_stats
        data_indices_pretrain = partition["data_indices"][int(args.pretrain_fraction*args.client_num_in_total):]
        data_indices_pretrain = np.concatenate(data_indices_pretrain)
        num_train_samples = int(len(data_indices_pretrain) * args.fraction)
        np.random.shuffle(data_indices_pretrain)
        partition["data_indices_pretrain"] = {
            "train": data_indices_pretrain[:num_train_samples],
            "test": data_indices_pretrain[num_train_samples:],
        }

        partition["data_indices"] = partition["data_indices"][:int(args.pretrain_fraction*args.client_num_in_total)]
        args.client_num_in_total = int(args.client_num_in_total*round(1-args.pretrain_fraction, 2))

    print(f"Stats: {stats}")
    
    if partition["separation"] is None:
        if args.split == "user":
            train_clients_num = int(args.client_num_in_total * args.fraction)
            clients_4_train = list(range(train_clients_num))
            clients_4_test = list(range(train_clients_num, args.client_num_in_total))
        else:
            clients_4_train = list(range(args.client_num_in_total))
            clients_4_test = list(range(args.client_num_in_total))

        partition["separation"] = {
            "train": clients_4_train,
            "test": clients_4_test,
            "total": args.client_num_in_total,
        }

    if args.dataset not in ["femnist", "celeba"]:
        for client_id, idx in enumerate(partition["data_indices"]):
            if args.split == "sample":
                num_train_samples = int(len(idx) * args.fraction)
                np.random.shuffle(idx)
                idx_train, idx_test = idx[:num_train_samples], idx[num_train_samples:]
                partition["data_indices"][client_id] = {
                    "train": idx_train,
                    "test": idx_test,
                }
            else:
                if client_id in clients_4_train:
                    partition["data_indices"][client_id] = {"train": idx, "test": []}
                else:
                    partition["data_indices"][client_id] = {"train": [], "test": idx}

    print("Writing partition file...")
    with open(_CURRENT_DIR.parent / args.dataset / "partition.pkl", "wb") as f:
        pickle.dump(partition, f)

    with open(_CURRENT_DIR.parent / args.dataset / "all_stats.json", "w") as f:
        json.dump(stats, f)

    with open(_CURRENT_DIR.parent / args.dataset / "args.json", "w") as f:
        json.dump(prune_args(args), f)
    print("Done!")


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument(
        "-d",
        "--dataset",
        type=str,
        choices=[
            "mnist",
            "cifar10",
            "cifar100",
            "synthetic",
            "femnist",
            "emnist",
            "fmnist",
            "celeba",
            "medmnistS",
            "medmnistA",
            "medmnistC",
            "covid19",
            "svhn",
            "usps",
            "tiny_imagenet",
            "cinic10",
        ],
        default="cifar100",
    )
    parser.add_argument("--iid", type=int, default=0)
    parser.add_argument("-cn", "--client_num_in_total", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--split", type=str, choices=["sample", "user"], default="sample"
    )
    parser.add_argument(
        "--fraction", type=float, default=0.8, help="Propotion of train data/clients"
    )
    parser.add_argument(
        "--pretrain_fraction", type=float, default=0.8, help="Propotion of pretrain data"
    )
    # For random assigning classes only
    parser.add_argument(
        "-c",
        "--classes",
        type=int,
        default=0,
        help="Num of classes that one client's data belong to.",
    )
    # For allocate shards only
    parser.add_argument(
        "-s",
        "--shards",
        type=int,
        default=0,
        help="Num of classes that one client's data belong to.",
    )
    # For dirichlet distribution only
    parser.add_argument(
        "-a",
        "--alpha",
        type=float,
        default=0.5,
        help="Only for controling data hetero degree while performing Dirichlet partition.",
    )
    parser.add_argument("-ls", "--least_samples", type=int, default=40)

    # For synthetic data only
    parser.add_argument("--gamma", type=float, default=0.5)
    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--dimension", type=int, default=60)

    # For CIFAR-100 only
    parser.add_argument("--super_class", type=int, default=0)

    # For EMNIST only
    parser.add_argument(
        "--emnist_split",
        type=str,
        choices=["byclass", "bymerge", "letters", "balanced", "digits", "mnist"],
        default="byclass",
    )
    args = parser.parse_args()
    main(args)
