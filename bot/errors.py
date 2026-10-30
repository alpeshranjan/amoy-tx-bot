class PolicyError(Exception):
    """The message cannot become a transfer."""


class StartupError(Exception):
    """The relayer ledger does not match the chain. Refuse to run."""


class UserReject(Exception):
    """This queued item cannot be signed. No nonce is allocated."""


class LowPol(Exception):
    """The relayer does not have enough POL to sign."""


class FeeCeiling(Exception):
    """A fee bump would pass the configured maximum."""
