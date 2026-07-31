import evalgate_api


def test_package_imports_and_exposes_version() -> None:
    assert isinstance(evalgate_api.__version__, str)
    assert evalgate_api.__version__
