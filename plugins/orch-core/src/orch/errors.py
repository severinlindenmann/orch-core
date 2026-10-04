class OrchError(Exception):
    exit_code = 1

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(OrchError):
    exit_code = 2


class NotFoundError(UsageError):
    pass


class TransitionError(OrchError):
    exit_code = 3


class HumanOnlyError(TransitionError):
    pass


class ClaimError(OrchError):
    exit_code = 4


class LockBusyError(ClaimError):
    pass


class ValidationError(OrchError):
    exit_code = 5


class TicketParseError(OrchError):
    exit_code = 6


class WaitTimeout(OrchError):
    exit_code = 7
