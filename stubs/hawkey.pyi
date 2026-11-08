import builtins

from dnf import Sack

class Exception(builtins.Exception): ...

class Reldep:
    name: str
    relation: str
    version: str

class NEVRA:
    name: str
    epoch: int | None
    version: str
    release: str
    arch: str
    def evr_cmp(self, other: NEVRA, sack: Sack) -> int: ...

def split_nevra(nevra: str) -> NEVRA: ...
def chksum_type(name: str) -> int: ...
