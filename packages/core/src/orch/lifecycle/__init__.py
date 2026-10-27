"""``orch stop`` / ``orch start`` / ``orch services``: stop and start what orch runs in the background on this computer.

Stopped means stopped until ``orch start``, also after a reboot (the unit is disabled, not removed); nothing is
deleted (that is ``orch host prune``). The discovery reads the same unit names and install record as prune."""
