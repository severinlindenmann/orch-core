"""File Explorer add-on (issue #491): read-only look at a workspace's files and their git history, for owners.

`guard` is the ONE place every read of a file or folder goes through (containment, deny list, no symlink followed).
`gitread` runs git through orch's closed `gitsafe` operations; `search` runs ripgrep or git grep; `links` derives the
ticket chips from host events; `routes` is the HTTP surface.
"""
