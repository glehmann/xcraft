from dnf import ConfigMain

class Substitutions(dict[str, str]): ...

class Conf:
    _config: ConfigMain
    substitutions: Substitutions
    installroot: str
    cachedir: str
    install_weak_deps: bool
    exclude_from_weak: list[str]
    def read(self, filename: str | None = None) -> None: ...
