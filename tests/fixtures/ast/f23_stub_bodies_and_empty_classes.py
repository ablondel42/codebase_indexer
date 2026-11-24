import builtins


def stub_pass() -> None:
    pass


def stub_ellipsis() -> None:
    ...


def stub_doc_only() -> None:
    """Only a docstring."""


def stub_doc_pass() -> None:
    """Doc."""
    pass


def stub_doc_ellipsis() -> None:
    """Doc."""
    ...


def stub_pass_ellipsis() -> None:
    pass
    ...


def stub_not_impl() -> None:
    raise NotImplementedError


def stub_not_impl_call() -> None:
    raise NotImplementedError()


def stub_not_impl_msg() -> None:
    raise NotImplementedError("override me")


def stub_builtins_not_impl() -> None:
    raise builtins.NotImplementedError("override")


def stub_doc_not_impl() -> None:
    """Doc."""
    raise NotImplementedError


class EmptyPass:
    pass


class DocOnlyClass:
    """Class doc."""


class EllipsisClass:
    ...
