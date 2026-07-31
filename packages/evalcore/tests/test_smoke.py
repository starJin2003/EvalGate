import evalcore


def test_package_imports_and_exposes_version() -> None:
    assert isinstance(evalcore.__version__, str)
    assert evalcore.__version__
