class DomainError(Exception):
    """Ошибка бизнес-логики. Текст показывается пользователю как есть."""


class NotFound(DomainError):
    pass


class AccessDenied(DomainError):
    pass


class ValidationError(DomainError):
    pass


class Conflict(DomainError):
    pass


class InsufficientStock(DomainError):
    pass


class InsufficientFunds(DomainError):
    pass
