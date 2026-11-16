class LakeError(Exception):
    """Base class for public business errors."""


class SchemaError(LakeError):
    pass


class DataError(LakeError):
    pass


class ResolutionError(LakeError):
    pass


class NotFound(LakeError):
    pass


class Conflict(LakeError):
    pass
