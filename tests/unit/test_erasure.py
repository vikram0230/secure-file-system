import itertools
import os

import pytest

from api_service.services.erasure import (
    NotEnoughShardsError,
    decode,
    encode,
    regenerate,
)

K, M = 4, 2


@pytest.mark.parametrize("size", [1, 3, 4, 5, 1023, 1024, 65537])
def test_round_trip_all_sizes(size):
    data = os.urandom(size)
    blocks = encode(data, K, M)

    assert len(blocks) == K + M
    assert len({len(b) for b in blocks}) == 1
    assert decode(dict(enumerate(blocks)), K, M, size) == data


def test_primary_blocks_are_the_data():
    data = os.urandom(4000)
    blocks = encode(data, K, M)
    assert b"".join(blocks[:K]) == data


def test_any_k_of_n_reconstructs():
    data = os.urandom(10_001)
    blocks = encode(data, K, M)

    for survivors in itertools.combinations(range(K + M), K):
        subset = {i: blocks[i] for i in survivors}
        assert decode(subset, K, M, len(data)) == data, survivors


def test_fewer_than_k_raises():
    blocks = encode(b"payload", K, M)
    with pytest.raises(NotEnoughShardsError):
        decode({0: blocks[0], 5: blocks[5], 3: blocks[3]}, K, M, 7)


def test_empty_data_rejected():
    with pytest.raises(ValueError):
        encode(b"", K, M)


@pytest.mark.parametrize("lost", [(0,), (5,), (1, 4), (2, 3)])
def test_regenerate_lost_blocks_matches_original(lost):
    data = os.urandom(9999)
    blocks = encode(data, K, M)
    survivors = {i: b for i, b in enumerate(blocks) if i not in lost}

    rebuilt = regenerate(survivors, K, M, lost)

    assert rebuilt == [blocks[i] for i in lost]


@pytest.mark.parametrize(("k", "m"), [(1, 1), (2, 1), (6, 3), (10, 4)])
def test_other_schemes(k, m):
    data = os.urandom(5000)
    blocks = encode(data, k, m)
    survivors = dict(list(enumerate(blocks))[m:])
    assert decode(survivors, k, m, len(data)) == data
