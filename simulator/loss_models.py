"""Packet loss models for network simulation.

Provides realistic loss patterns beyond simple independent random loss.
"""
import random
from typing import Callable


def independent_loss(rate: float) -> Callable[[int], float]:
    """Create an independent (memoryless) loss model.

    Each packet has the same probability of being lost, independent of
    previous packets. Simple but unrealistic for most networks.

    Args:
        rate: Loss probability [0.0, 1.0]

    Returns:
        Function that returns loss probability (always `rate`)
    """
    def loss_func(packet_id: int = None) -> float:
        return rate
    return loss_func


class GilbertElliottLoss:
    """Two-state Markov loss model for realistic bursty packet loss.

    The Gilbert-Elliott model represents a channel that alternates between
    "good" and "bad" states. In the good state, packets are rarely lost.
    In the bad state, packets are frequently lost. This models real network
    behavior where losses tend to occur in bursts.

    State transitions:
        GOOD --[p_good_to_bad]--> BAD
        BAD --[p_bad_to_good]--> GOOD

    References:
        - Gilbert, E. N. (1960). "Capacity of a burst-noise channel"
        - Elliott, E. O. (1963). "Estimates of error rates for codes on burst-noise channels"
    """

    def __init__(self,
                 p_good_to_bad: float = 0.02,
                 p_bad_to_good: float = 0.3,
                 loss_in_good: float = 0.0,
                 loss_in_bad: float = 0.5,
                 seed: int = None):
        """Initialize Gilbert-Elliott loss model.

        Args:
            p_good_to_bad: Probability of transitioning from good to bad state
            p_bad_to_good: Probability of transitioning from bad to good state
            loss_in_good: Packet loss probability in good state
            loss_in_bad: Packet loss probability in bad state
            seed: Random seed for reproducibility
        """
        self.p_good_to_bad = p_good_to_bad
        self.p_bad_to_good = p_bad_to_good
        self.loss_in_good = loss_in_good
        self.loss_in_bad = loss_in_bad
        self.state = 'good'
        self._rng = random.Random(seed)

    def __call__(self, packet_id: int = None) -> float:
        """Get loss probability for next packet.

        Transitions state based on Markov probabilities, then returns
        the loss probability for the current state.

        Args:
            packet_id: Ignored (for API compatibility with ns.py)

        Returns:
            Loss probability for this packet
        """
        # State transition
        if self.state == 'good':
            if self._rng.random() < self.p_good_to_bad:
                self.state = 'bad'
        else:
            if self._rng.random() < self.p_bad_to_good:
                self.state = 'good'

        # Return loss probability for current state
        return self.loss_in_good if self.state == 'good' else self.loss_in_bad

    def reset(self) -> None:
        """Reset to good state."""
        self.state = 'good'

    @classmethod
    def from_average_loss(cls,
                          target_loss_rate: float,
                          burst_length: float = 3.0,
                          loss_in_bad: float = 0.8,
                          seed: int = None) -> 'GilbertElliottLoss':
        """Create model targeting a specific average loss rate.

        Calculates transition probabilities to achieve the desired
        average loss rate with specified burst characteristics.

        Args:
            target_loss_rate: Desired average packet loss rate
            burst_length: Average number of packets lost in a burst
            loss_in_bad: Loss probability in bad state
            seed: Random seed

        Returns:
            Configured GilbertElliottLoss instance
        """
        # Average time in bad state = 1 / p_bad_to_good
        # We want this to equal burst_length
        p_bad_to_good = 1.0 / burst_length

        # Steady-state probability of being in bad state:
        # P(bad) = p_good_to_bad / (p_good_to_bad + p_bad_to_good)
        #
        # Average loss = P(good) * loss_in_good + P(bad) * loss_in_bad
        #              ≈ P(bad) * loss_in_bad  (assuming loss_in_good ≈ 0)
        #
        # So: target_loss_rate ≈ P(bad) * loss_in_bad
        #     P(bad) = target_loss_rate / loss_in_bad
        #
        # Solving for p_good_to_bad:
        # P(bad) = p_good_to_bad / (p_good_to_bad + p_bad_to_good)
        # p_good_to_bad = P(bad) * p_bad_to_good / (1 - P(bad))

        p_bad = min(0.99, target_loss_rate / loss_in_bad)
        if p_bad >= 1.0:
            # Can't achieve target with given loss_in_bad
            p_good_to_bad = 0.5
        else:
            p_good_to_bad = p_bad * p_bad_to_good / (1 - p_bad)

        return cls(
            p_good_to_bad=min(1.0, p_good_to_bad),
            p_bad_to_good=p_bad_to_good,
            loss_in_good=0.0,
            loss_in_bad=loss_in_bad,
            seed=seed
        )


class SimpleBurstLoss:
    """Simple burst loss model matching current network_config behavior.

    When entering burst mode, drops a fixed number of consecutive packets.
    Simpler than Gilbert-Elliott but matches existing test expectations.
    """

    def __init__(self,
                 burst_probability: float = 0.0,
                 burst_length: int = 3,
                 seed: int = None):
        """Initialize simple burst loss model.

        Args:
            burst_probability: Probability of starting a new burst
            burst_length: Number of consecutive packets to drop in burst
            seed: Random seed
        """
        self.burst_probability = burst_probability
        self.burst_length = burst_length
        self._remaining = 0
        self._rng = random.Random(seed)

    def __call__(self, packet_id: int = None) -> float:
        """Get loss probability for next packet.

        Returns 1.0 (certain loss) during a burst, 0.0 otherwise.
        May probabilistically start a new burst.
        """
        # If in burst, continue dropping
        if self._remaining > 0:
            self._remaining -= 1
            return 1.0

        # Check if we should start a new burst
        if self._rng.random() < self.burst_probability:
            self._remaining = self.burst_length - 1  # -1 because we drop this one
            return 1.0

        return 0.0

    def reset(self) -> None:
        """Reset burst state."""
        self._remaining = 0


class CombinedLoss:
    """Combines multiple loss models (e.g., random + burst)."""

    def __init__(self, *models: Callable[[int], float]):
        """Initialize with multiple loss models.

        A packet is lost if ANY model says to drop it.
        """
        self.models = models

    def __call__(self, packet_id: int = None) -> float:
        """Get combined loss probability.

        Uses union probability: P(A or B) = P(A) + P(B) - P(A)*P(B)
        """
        p_keep = 1.0
        for model in self.models:
            p_loss = model(packet_id)
            p_keep *= (1.0 - p_loss)
        return 1.0 - p_keep
