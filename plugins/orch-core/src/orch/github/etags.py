"""Memory only: a file would hold bodies and create a second tamperable cache boundary."""

from __future__ import annotations

import copy
import json
from collections import OrderedDict


class EtagStore:
    def __init__(self, max_entries=500, max_bytes=8 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries = OrderedDict()
        self.size_bytes = 0

    def get(self, fp, key):
        pair = (fp, key)
        if pair not in self._entries:
            return None
        self._entries.move_to_end(pair)
        etag, body, _ = self._entries[pair]
        return etag, copy.deepcopy(body)

    def drop(self, fp, key):
        entry = self._entries.pop((fp, key), None)
        if entry is not None:
            self.size_bytes -= entry[2]

    def put(self, fp, key, etag, body):
        self.drop(fp, key)
        size = len(json.dumps(body, ensure_ascii=True))
        if size > self.max_bytes or self.max_entries < 1:
            return
        self._entries[fp, key] = (etag, copy.deepcopy(body), size)
        self.size_bytes += size
        while len(self._entries) > self.max_entries or self.size_bytes > self.max_bytes:
            _, (_, _, size) = self._entries.popitem(last=False)
            self.size_bytes -= size

    def drop_other_tokens(self, fp):
        for fingerprint, key in list(self._entries):
            if fingerprint != fp:
                self.drop(fingerprint, key)

    def clear(self):
        self._entries.clear()
        self.size_bytes = 0
