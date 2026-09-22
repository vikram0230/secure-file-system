"""k-of-n erasure coding over zfec.

A file is zero-padded to a multiple of k bytes and split into k equal primary
blocks; zfec derives m parity blocks. Blocks 0..k-1 are the primary blocks
verbatim, so decoding from them is a concatenation.
"""

from collections.abc import Mapping, Sequence

import zfec


class NotEnoughShardsError(Exception):
    pass


def encode(data: bytes, k: int, m: int) -> list[bytes]:
    """Return k + m blocks; any k of them reconstruct ``data``."""
    if not data:
        raise ValueError("cannot encode empty data")
    block_size = -(-len(data) // k)
    padded = data.ljust(block_size * k, b"\0")
    primary = [padded[i * block_size : (i + 1) * block_size] for i in range(k)]
    return list(zfec.Encoder(k, k + m).encode(primary))


def decode_primary(blocks: Mapping[int, bytes], k: int, m: int) -> list[bytes]:
    """Recover the k primary blocks from any k (index -> block) entries."""
    if len(blocks) < k:
        raise NotEnoughShardsError(f"need {k} shards, have {len(blocks)}")
    indices = sorted(blocks)[:k]
    if any(not 0 <= i < k + m for i in indices):
        raise ValueError("shard index out of range")
    return list(zfec.Decoder(k, k + m).decode([blocks[i] for i in indices], indices))


def decode(blocks: Mapping[int, bytes], k: int, m: int, size: int) -> bytes:
    """Reassemble the original ``size`` bytes from any k blocks."""
    return b"".join(decode_primary(blocks, k, m))[:size]


def regenerate(blocks: Mapping[int, bytes], k: int, m: int, indices: Sequence[int]) -> list[bytes]:
    """Rebuild specific blocks (e.g. lost shards) from any k surviving ones."""
    primary = decode_primary(blocks, k, m)
    return list(zfec.Encoder(k, k + m).encode(primary, list(indices)))
