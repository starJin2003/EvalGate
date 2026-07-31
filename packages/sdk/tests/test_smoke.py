import evalgate_sdk


def test_package_imports_and_exposes_version() -> None:
    assert isinstance(evalgate_sdk.__version__, str)
    assert evalgate_sdk.__version__
