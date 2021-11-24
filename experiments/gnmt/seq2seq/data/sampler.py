import logging

import torch
from torch.utils.data.sampler import Sampler

from gnmt.seq2seq.utils import get_rank
from gnmt.seq2seq.utils import get_world_size
from gnmt.seq2seq.utils import gnmt_print


class DistributedSampler(Sampler):
    def __init__(self, dataset, batch_size, seeds, world_size=None, rank=None):
        """
        Constructor for the DistributedSampler.

        :param dataset: dataset
        :param batch_size: local batch size
        :param seeds: list of seeds, one seed for each training epoch
        :param world_size: number of distributed workers
        :param rank: rank of the current process
        """
        if world_size is None:
            world_size = get_world_size()
        if rank is None:
            rank = get_rank()

        self.dataset = dataset
        self.world_size = world_size
        self.rank = rank
        self.epoch = 0
        self.seeds = seeds

        self.batch_size = batch_size
        self.global_batch_size = batch_size * world_size

        self.data_len = len(self.dataset)

        self.num_samples = self.data_len // self.global_batch_size \
            * self.global_batch_size

    def init_rng(self):
        """
        Creates new RNG, seed depends on current epoch idx.
        """
        rng = torch.Generator()
        seed = self.seeds[self.epoch]
        logging.info(f'Sampler for epoch {self.epoch} uses seed {seed}')
        rng.manual_seed(seed)
        return rng

    def distribute_batches(self, indices):
        """
        Assigns batches to workers.
        Consecutive ranks are getting consecutive batches.

        :param indices: torch.tensor with batch indices
        """
        assert len(indices) == self.num_samples

        indices = indices.view(-1, self.batch_size)
        indices = indices[self.rank::self.world_size].contiguous()
        indices = indices.view(-1)
        indices = indices.tolist()

        assert len(indices) == self.num_samples // self.world_size
        return indices

    def reshuffle_batches(self, indices, rng):
        """
        Permutes global batches

        :param indices: torch.tensor with batch indices
        :param rng: instance of torch.Generator
        """
        indices = indices.view(-1, self.global_batch_size)
        num_batches = indices.shape[0]
        order = torch.randperm(num_batches, generator=rng)
        indices = indices[order, :]
        indices = indices.view(-1)
        return indices

    def __iter__(self):
        rng = self.init_rng()
        # generate permutation
        indices = torch.randperm(self.data_len, generator=rng)

        # make indices evenly divisible by (batch_size * world_size)
        indices = indices[:self.num_samples]

        # assign batches to workers
        indices = self.distribute_batches(indices)
        return iter(indices)

    def set_epoch(self, epoch):
        """
        Sets current epoch index.
        Epoch index is used to seed RNG in __iter__() function.

        :param epoch: index of current epoch
        """
        self.epoch = epoch

    def __len__(self):
        return self.num_samples // self.world_size


class ShardingSampler(DistributedSampler):
    def __init__(self, dataset, batch_size, seeds, shard_size,
                 world_size=None, rank=None):
        """
        Constructor for the ShardingSampler.

        :param dataset: dataset
        :param batch_size: local batch size
        :param seeds: list of seeds, one seed for each training epoch
        :param shard_size: number of global batches within one shard
        :param world_size: number of distributed workers
        :param rank: rank of the current process
        """

        super().__init__(dataset, batch_size, seeds, world_size, rank)

        self.shard_size = shard_size
        self.num_samples = self.data_len // self.global_batch_size \
            * self.global_batch_size

    def __iter__(self):
        rng = self.init_rng()
        # generate permutation
        indices = torch.randperm(self.data_len, generator=rng)
        # make indices evenly divisible by (batch_size * world_size)
        indices = indices[:self.num_samples]

        # splits the dataset into chunks of 'self.shard_size' global batches
        # each, sorts by (src + tgt) sequence length within each chunk,
        # reshuffles all global batches
        shard_size = self.global_batch_size * self.shard_size
        nshards = (self.num_samples + shard_size - 1) // shard_size

        lengths = self.dataset.lengths[indices]

        shards = [indices[i * shard_size:(i+1) * shard_size] for i in range(nshards)]
        len_shards = [lengths[i * shard_size:(i+1) * shard_size] for i in range(nshards)]

        # sort by (src + tgt) sequence length within each shard
        indices = []
        for len_shard in len_shards:
            _, ind = len_shard.sort()
            indices.append(ind)

        output = tuple(shard[idx] for shard, idx in zip(shards, indices))

        # build batches
        indices = torch.cat(output)
        # perform global reshuffle of all global batches
        indices = self.reshuffle_batches(indices, rng)
        # distribute batches to individual workers
        indices = self.distribute_batches(indices)
        return iter(indices)


class BucketingSampler(DistributedSampler):
    def __init__(self, dataset, batch_size, seeds, num_buckets,
                 world_size=None, rank=None):
        """
        Constructor for the BucketingSampler.

        :param dataset: dataset
        :param batch_size: local batch size
        :param seeds: list of seeds, one seed for each training epoch
        :param num_buckets: number of buckets
        :param world_size: number of distributed workers
        :param rank: rank of the current process
        """

        super().__init__(dataset, batch_size, seeds, world_size, rank)

        self.num_buckets = num_buckets
        bucket_width = (dataset.max_len + num_buckets - 1) // num_buckets

        # assign sentences to buckets based on src and tgt sequence lengths
        bucket_ids = torch.max(dataset.src_lengths // bucket_width,
                               dataset.tgt_lengths // bucket_width)
        bucket_ids.clamp_(0, num_buckets - 1)

        # build buckets
        all_indices = torch.tensor(range(self.data_len))
        self.buckets = []
        self.num_samples = 0
        global_bs = self.global_batch_size

        for bid in range(num_buckets):
            # gather indices for current bucket
            indices = all_indices[bucket_ids == bid]
            self.buckets.append(indices)

            # count number of samples in current bucket
            samples = len(indices) // global_bs * global_bs
            self.num_samples += samples

    def __iter__(self):
        rng = self.init_rng()
        global_bs = self.global_batch_size

        indices = []
        for bid in range(self.num_buckets):
            # random shuffle within current bucket
            perm = torch.randperm(len(self.buckets[bid]), generator=rng)
            bucket_indices = self.buckets[bid][perm]

            # make bucket_indices evenly divisible by global batch size
            length = len(bucket_indices) // global_bs * global_bs
            bucket_indices = bucket_indices[:length]
            assert len(bucket_indices) % self.global_batch_size == 0

            # add samples from current bucket to indices for current epoch
            indices.append(bucket_indices)

        indices = torch.cat(indices)
        assert len(indices) % self.global_batch_size == 0

        # perform global reshuffle of all global batches
        indices = self.reshuffle_batches(indices, rng)
        # distribute batches to individual workers
        indices = self.distribute_batches(indices)
        return iter(indices)


class StaticDistributedSampler(Sampler):
    def __init__(self, dataset, batch_size, pad, world_size=None, rank=None):
        """
        Constructor for the StaticDistributedSampler.

        :param dataset: dataset
        :param batch_size: local batch size
        :param pad: if True: pads dataset to a multiple of global_batch_size
            samples
        :param world_size: number of distributed workers
        :param rank: rank of the current process
        """
        if world_size is None:
            world_size = get_world_size()
        if rank is None:
            rank = get_rank()

        self.world_size = world_size

        global_batch_size = batch_size * world_size

        data_len = len(dataset)
        num_samples = (data_len + global_batch_size - 1) \
            // global_batch_size * global_batch_size
        self.num_samples = num_samples

        indices = list(range(data_len))
        if pad:
            # pad dataset to a multiple of global_batch_size samples, uses
            # sample with idx 0 as pad
            indices += [0] * (num_samples - len(indices))
        else:
            # temporary pad to a multiple of global batch size, pads with "-1"
            # which is later removed from the list of indices
            indices += [-1] * (num_samples - len(indices))
        indices = torch.tensor(indices)

        indices = indices.view(-1, batch_size)
        indices = indices[rank::world_size].contiguous()
        indices = indices.view(-1)
        # remove temporary pad
        indices = indices[indices != -1]
        indices = indices.tolist()
        self.indices = indices

    def __iter__(self):
        return iter(self.indices)

    def __len__(self):
        return len(self.indices)