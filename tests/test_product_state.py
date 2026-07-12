from __future__ import annotations

import unittest

from core.product_state import (
    ConnectivityState,
    InvalidStateTransition,
    LicenseState,
    PaymentState,
    UpdateState,
    allowed_transitions,
    can_transition,
    transition_or_raise,
)


class ProductStateTests(unittest.TestCase):
    def test_required_license_and_payment_states_are_present(self):
        self.assertEqual({state.value for state in LicenseState}, {"missing", "active"})
        self.assertEqual(
            {state.value for state in PaymentState},
            {"pending", "under_review", "approved", "rejected"},
        )

    def test_required_update_states_are_present(self):
        required = {
            "old_version",
            "available",
            "downloading",
            "installing",
            "installed",
            "failed",
            "rolled_back",
        }

        self.assertTrue(required.issubset({state.value for state in UpdateState}))

    def test_required_connectivity_states_are_present(self):
        self.assertEqual(
            {state.value for state in ConnectivityState},
            {"online", "offline", "server_unavailable"},
        )

    def test_license_activation_is_one_way(self):
        self.assertTrue(can_transition(LicenseState.MISSING, LicenseState.ACTIVE))
        self.assertFalse(can_transition(LicenseState.ACTIVE, LicenseState.MISSING))

    def test_payment_review_can_approve_or_reject(self):
        self.assertTrue(
            can_transition(PaymentState.PENDING, PaymentState.UNDER_REVIEW)
        )
        self.assertTrue(
            can_transition(PaymentState.UNDER_REVIEW, PaymentState.APPROVED)
        )
        self.assertTrue(
            can_transition(PaymentState.UNDER_REVIEW, PaymentState.REJECTED)
        )
        self.assertFalse(can_transition(PaymentState.APPROVED, PaymentState.PENDING))
        self.assertFalse(can_transition(PaymentState.REJECTED, PaymentState.APPROVED))

    def test_update_purchase_and_install_happy_path(self):
        states = (
            UpdateState.CURRENT,
            UpdateState.AVAILABLE,
            UpdateState.PURCHASE_REQUIRED,
            UpdateState.ENTITLED,
            UpdateState.DOWNLOADING,
            UpdateState.VERIFYING,
            UpdateState.INSTALLING,
            UpdateState.INSTALLED,
            UpdateState.CURRENT,
        )

        for current, target in zip(states, states[1:]):
            with self.subTest(current=current, target=target):
                self.assertTrue(can_transition(current, target))
                self.assertIs(transition_or_raise(current, target), target)

    def test_old_version_can_remain_working_without_purchase(self):
        self.assertTrue(can_transition(UpdateState.OLD_VERSION, UpdateState.AVAILABLE))
        self.assertTrue(
            can_transition(UpdateState.AVAILABLE, UpdateState.PURCHASE_REQUIRED)
        )
        self.assertTrue(
            can_transition(UpdateState.PURCHASE_REQUIRED, UpdateState.OLD_VERSION)
        )

    def test_failed_install_can_retry_or_roll_back(self):
        self.assertTrue(can_transition(UpdateState.INSTALLING, UpdateState.FAILED))
        self.assertTrue(can_transition(UpdateState.FAILED, UpdateState.ENTITLED))
        self.assertTrue(can_transition(UpdateState.FAILED, UpdateState.ROLLED_BACK))
        self.assertTrue(
            can_transition(UpdateState.ROLLED_BACK, UpdateState.OLD_VERSION)
        )

    def test_connectivity_recovers_from_offline_and_server_failure(self):
        self.assertTrue(
            can_transition(ConnectivityState.ONLINE, ConnectivityState.OFFLINE)
        )
        self.assertTrue(
            can_transition(ConnectivityState.OFFLINE, ConnectivityState.ONLINE)
        )
        self.assertTrue(
            can_transition(
                ConnectivityState.ONLINE,
                ConnectivityState.SERVER_UNAVAILABLE,
            )
        )
        self.assertTrue(
            can_transition(
                ConnectivityState.SERVER_UNAVAILABLE,
                ConnectivityState.ONLINE,
            )
        )

    def test_known_same_state_is_an_idempotent_no_op(self):
        self.assertTrue(can_transition(UpdateState.DOWNLOADING, UpdateState.DOWNLOADING))
        self.assertIs(
            transition_or_raise(UpdateState.DOWNLOADING, UpdateState.DOWNLOADING),
            UpdateState.DOWNLOADING,
        )

    def test_invalid_transitions_fail_closed(self):
        invalid = (
            (LicenseState.ACTIVE, LicenseState.MISSING),
            (PaymentState.APPROVED, PaymentState.UNDER_REVIEW),
            (UpdateState.INSTALLED, UpdateState.DOWNLOADING),
            (ConnectivityState.OFFLINE, ConnectivityState.SERVER_UNAVAILABLE),
            (LicenseState.ACTIVE, PaymentState.APPROVED),
            ("active", "missing"),
        )

        for current, target in invalid:
            with self.subTest(current=current, target=target):
                self.assertFalse(can_transition(current, target))
                with self.assertRaises(InvalidStateTransition):
                    transition_or_raise(current, target)

    def test_unknown_state_has_no_allowed_transitions(self):
        self.assertEqual(allowed_transitions("active"), frozenset())
        transitions = allowed_transitions(UpdateState.FAILED)
        self.assertIsInstance(transitions, frozenset)
        self.assertEqual(
            transitions,
            frozenset({UpdateState.ENTITLED, UpdateState.ROLLED_BACK}),
        )


if __name__ == "__main__":
    unittest.main()
