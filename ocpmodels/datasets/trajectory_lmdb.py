"""
Copyright (c) Facebook, Inc. and its affiliates.

This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
"""

import bisect
import pickle
from pathlib import Path

import lmdb
import numpy as np
import torch
from torch.utils.data import Dataset
from torch_geometric.data import Batch

from ocpmodels.common import distutils
from ocpmodels.common.registry import registry


@registry.register_dataset("trajectory_lmdb")
class TrajectoryLmdbDataset(Dataset):
    r"""Dataset class to load from LMDB files containing relaxation trajectories.
    Useful for Structure to Energy & Force (S2EF) and Initial State to
    Relaxed State (IS2RS) tasks.

    Args:
        config (dict): Dataset configuration
        transform (callable, optional): Data transform function.
            (default: :obj:`None`)
    """

    def __init__(self, config, transform=None):
        super(TrajectoryLmdbDataset, self).__init__()
        self.config = config

        # If running in distributed mode, only read a subset of database files
        world_size = distutils.get_world_size()
        rank = distutils.get_rank()
        srcdir = Path(self.config["src"])
        db_paths = sorted(srcdir.glob("*.lmdb"))
        assert len(db_paths) > 0, f"No LMDBs found in {srcdir}"
        # Each process only reads a subset of the DB files. However, since the
        # number of DB files may not be divisible by world size, the final
        # (num_dbs % world_size) are shared by all processes.
        num_full_dbs = len(db_paths) - (len(db_paths) % world_size)
        full_db_paths = db_paths[rank:num_full_dbs:world_size]
        shared_db_paths = db_paths[num_full_dbs:]
        self.db_paths = full_db_paths + shared_db_paths

        self._keys, self.envs = [], []
        for db_path in full_db_paths:
            self.envs.append(self.connect_db(db_path))
            length = pickle.loads(
                self.envs[-1].begin().get("length".encode("ascii"))
            )
            self._keys.append(list(range(length)))
        for db_path in shared_db_paths:
            self.envs.append(self.connect_db(db_path))
            length = pickle.loads(
                self.envs[-1].begin().get("length".encode("ascii"))
            )
            length -= length % world_size
            self._keys.append(list(range(rank, length, world_size)))
        self._keylens = [len(k) for k in self._keys]
        self._keylen_cumulative = np.cumsum(self._keylens).tolist()
        self.num_samples = sum(self._keylens)
        self.transform = transform

    def __len__(self):
        return self.num_samples

    def __getitem__(self, idx):
        # Figure out which db this should be indexed from.
        db_idx = bisect.bisect(self._keylen_cumulative, idx)
        # Extract index of element within that db.
        el_idx = idx
        if db_idx != 0:
            el_idx = idx - self._keylen_cumulative[db_idx - 1]
        assert el_idx >= 0

        # Return features.
        datapoint_pickled = self.envs[db_idx].begin().get(
            f"{self._keys[db_idx][el_idx]}".encode("ascii")
        )
        data_object = pickle.loads(datapoint_pickled)
        if self.transform is not None:
            data_object = self.transform(data_object)

        data_object.id = f"{db_idx}_{el_idx}"

        return data_object

    def connect_db(self, lmdb_path=None):
        env = lmdb.open(
            str(lmdb_path),
            subdir=False,
            readonly=True,
            lock=False,
            readahead=False,
            meminit=False,
            max_readers=1,
        )
        return env

    def close_db(self):
        self.env.close()


def data_list_collater(data_list, otf_graph=False):
    batch = Batch.from_data_list(data_list)

    if not otf_graph:
        try:
            n_neighbors = []
            for i, data in enumerate(data_list):
                n_index = data.edge_index[1, :]
                n_neighbors.append(n_index.shape[0])
            batch.neighbors = torch.tensor(n_neighbors)
        except NotImplementedError:
            print(
                "LMDB does not contain edge index information, set otf_graph=True"
            )
    return batch
