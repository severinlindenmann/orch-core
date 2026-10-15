"""The numeric bounds of section 0.11 (F10) for the reader and its children, in one place."""

from __future__ import annotations

MiB = 1 << 20

# -- children (host side)
MAX_CHILDREN = 4  # per host
PER_PRINCIPAL = 1  # at a time; others queue and count as in flight
WALL_SECONDS = 30.0
CPU_SECONDS = 10
ADDRESS_SPACE = 512 * MiB
OPEN_FILES = 64

# -- inputs
OBJECT_MAX = 64 * MiB  # an inflated object, equal to its header
DELTA_DEPTH = 64
PACKED_REFS_MAX = 32 * MiB
IDX_MAX = 256 * MiB
INDEX_MAX = 128 * MiB  # .git/index (tree.check, H10)
INDEX_ENTRIES = 1_000_000
WALK_ENTRIES = 200_000  # worktree walk (tree.check, H10)

# -- the reader's own bounds (not in section 0.11; chosen here and documented in the PR)
CONFIG_MAX = 1 * MiB
SMALL_FILE_MAX = 4096  # HEAD, a loose ref, a .git file, commondir
LOOSE_REFS_MAX = 100_000
PACKS_MAX = 256  # .idx files in objects/pack
OPEN_PACKS = 16  # pack fds kept open at once (OPEN_FILES is 64)
DELTA_BYTES_MAX = 128 * MiB  # delta data held while one chain is resolved
CACHE_BYTES = 64 * MiB  # resolved pack objects kept for delta bases
COMMITS_MAX = 500_000  # commits walked for one merge base
TREE_ENTRIES_MAX = 1_000_000  # paths compared by one diff
DIFF_FILES_MAX = 20_000  # entries in the files list of one diff
TEXT_BYTES_MAX = 8 * MiB  # a blob larger than this is "too large to diff" (never split into lines)
TEXT_LINES_MAX = 200_000  # lines per side of one file
REQUEST_LINES_MAX = 1_000_000  # lines diffed by one request, both sides of every file
COMMITS_SHOWN = 50  # base..head commits listed in a diff
RSS_MAX = 512 * MiB  # a child's resident memory, watched by the host (macOS has no RLIMIT_AS)
QUEUE_PER_PRINCIPAL = 8  # requests waiting for a child, per principal (section 0.11: 8 in flight)
QUEUE_MAX = 64  # requests waiting for a child, host-wide
REQUEST_MAX = 256 * 1024  # one request line on the child's stdin
RESPONSE_MAX = 16 * MiB  # the child's whole answer
PIN_BATCH = 64  # declared repos pinned by one child (pin_many, #471); more take more children
