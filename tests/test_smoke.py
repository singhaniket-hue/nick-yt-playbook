import rabbithole


def test_package_imports_with_version():
    assert rabbithole.__version__ == "0.1.0"
