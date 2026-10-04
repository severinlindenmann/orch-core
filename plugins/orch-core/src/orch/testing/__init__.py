"""Testing kit for orch addons (spec A1 §5.9): fakes, contracts and a fake workspace. Needs pytest only for the
pytest classes and fixtures."""
from orch.testing.contracts import (AddonContract, ProviderContract, SideEffect, check_provider, no_side_effects,
                                    provider_identity_problems, run_addon_check, run_addon_contract)
from orch.testing.fakes import PAYLOAD, FakeProvider, FakeRemote, FakeRunner, Recording, make_snapshot, sample_items
from orch.testing.workspace import FakeWorkspace, fake_workspace

__all__ = ["AddonContract", "FakeProvider", "FakeRemote", "FakeRunner", "FakeWorkspace", "PAYLOAD", "ProviderContract",
           "Recording", "SideEffect", "check_provider", "fake_workspace", "make_snapshot", "no_side_effects",
           "provider_identity_problems", "run_addon_check", "run_addon_contract", "sample_items"]
