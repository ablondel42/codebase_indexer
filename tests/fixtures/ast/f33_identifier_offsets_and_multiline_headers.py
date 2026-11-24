import urllib.parse as up
import xml.etree.ElementTree
import email . mime . text as emt

_ = "éé"; import urllib.request as ureq
_ = (up, xml, emt, ureq)


def _identity(fn):
    return fn


class   SpacedClass:
    pass


def    spaced_fn():
    pass


async    def    spaced_async():
    pass


class \
    MultilineClass:
    def assign_attrs(self) -> None:
        self.plain_attr = 1
        (
            self
            # comment between self and attr
        ).  spaced_attr = 2


@_identity
async \
    def \
    multiline_async():
    pass


def utf8_sig(prefix: str = "éé", target: int = 1) -> int:
    return len(prefix) + target
