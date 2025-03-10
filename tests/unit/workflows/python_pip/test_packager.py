import sys
from collections import namedtuple
from unittest import TestCase

import mock
import pytest

from aws_lambda_builders.workflows.python_pip.utils import OSUtils
from aws_lambda_builders.workflows.python_pip.compat import pip_no_compile_c_env_vars
from aws_lambda_builders.workflows.python_pip.compat import pip_no_compile_c_shim
from aws_lambda_builders.workflows.python_pip.packager import DependencyBuilder
from aws_lambda_builders.workflows.python_pip.packager import PythonPipDependencyBuilder
from aws_lambda_builders.workflows.python_pip.packager import Package
from aws_lambda_builders.workflows.python_pip.packager import PipRunner
from aws_lambda_builders.workflows.python_pip.packager import SubprocessPip
from aws_lambda_builders.workflows.python_pip.packager import get_lambda_abi
from aws_lambda_builders.workflows.python_pip.packager import InvalidSourceDistributionNameError
from aws_lambda_builders.workflows.python_pip.packager import NoSuchPackageError
from aws_lambda_builders.workflows.python_pip.packager import PackageDownloadError

from aws_lambda_builders.workflows.python_pip import packager

print(packager)

FakePipCall = namedtuple("FakePipEntry", ["args", "env_vars", "shim"])


class FakePip(object):
    def __init__(self):
        self._calls = []
        self._returns = []

    def main(self, args, env_vars=None, shim=None):
        self._calls.append(FakePipCall(args, env_vars, shim))
        if self._returns:
            return self._returns.pop(0)
        # Return an rc of 0 and an empty stderr and stdout
        return 0, b"", b""

    def add_return(self, return_pair):
        self._returns.append(return_pair)

    @property
    def calls(self):
        return self._calls


@pytest.fixture
def pip_factory():
    def create_pip_runner(osutils=None):
        pip = FakePip()
        pip_runner = PipRunner(python_exe=sys.executable, pip=pip, osutils=osutils)
        return pip, pip_runner

    return create_pip_runner


class CustomEnv(OSUtils):
    def __init__(self, env):
        self._env = env

    def environ(self):
        return self._env


@pytest.fixture
def osutils():
    return OSUtils()


class FakePopen(object):
    def __init__(self, rc, out, err):
        self.returncode = rc
        self._out = out
        self._err = err

    def communicate(self):
        return self._out, self._err


class FakePopenOSUtils(OSUtils):
    def __init__(self, processes):
        self.popens = []
        self._processes = processes

    def popen(self, *args, **kwargs):
        self.popens.append((args, kwargs))
        return self._processes.pop()


class TestGetLambdaAbi(object):
    def test_get_lambda_abi_python27(self):
        assert "cp27mu" == get_lambda_abi("python2.7")

    def test_get_lambda_abi_python36(self):
        assert "cp36m" == get_lambda_abi("python3.6")

    def test_get_lambda_abi_python37(self):
        assert "cp37m" == get_lambda_abi("python3.7")

    def test_get_lambda_abi_python38(self):
        assert "cp38" == get_lambda_abi("python3.8")


class TestPythonPipDependencyBuilder(object):
    def test_can_call_dependency_builder(self, osutils):
        mock_dep_builder = mock.Mock(spec=DependencyBuilder)
        osutils_mock = mock.Mock(spec=osutils)
        builder = PythonPipDependencyBuilder(
            osutils=osutils_mock, dependency_builder=mock_dep_builder, runtime="runtime"
        )
        builder.build_dependencies("artifacts/path/", "scratch_dir/path/", "path/to/requirements.txt")
        mock_dep_builder.build_site_packages.assert_called_once_with(
            "path/to/requirements.txt", "artifacts/path/", "scratch_dir/path/"
        )


class TestPackage(object):
    def test_can_create_package_with_custom_osutils(self, osutils):
        pkg = Package("", "foobar-1.0-py3-none-any.whl", osutils)
        assert pkg._osutils == osutils

    def test_wheel_package(self):
        filename = "foobar-1.0-py3-none-any.whl"
        pkg = Package("", filename)
        assert pkg.dist_type == "wheel"
        assert pkg.filename == filename
        assert pkg.identifier == "foobar==1.0"
        assert str(pkg) == "foobar==1.0(wheel)"

    def test_invalid_package(self):
        with pytest.raises(InvalidSourceDistributionNameError):
            Package("", "foobar.jpg")

    def test_diff_pkg_sdist_and_whl_do_not_collide(self):
        pkgs = set()
        pkgs.add(Package("", "foobar-1.0-py3-none-any.whl"))
        pkgs.add(Package("", "badbaz-1.0-py3-none-any.whl"))
        assert len(pkgs) == 2

    def test_same_pkg_is_eq(self):
        pkg = Package("", "foobar-1.0-py3-none-any.whl")
        assert pkg == pkg

    def test_pkg_is_eq_to_similar_pkg(self):
        pure_pkg = Package("", "foobar-1.0-py3-none-any.whl")
        plat_pkg = Package("", "foobar-1.0-py3-py36m-manylinux1_x86_64.whl")
        assert pure_pkg == plat_pkg

    def test_pkg_is_not_equal_to_different_type(self):
        pkg = Package("", "foobar-1.0-py3-none-any.whl")
        non_package_type = 1
        assert not (pkg == non_package_type)

    def test_pkg_repr(self):
        pkg = Package("", "foobar-1.0-py3-none-any.whl")
        assert repr(pkg) == "foobar==1.0(wheel)"

    def test_wheel_data_dir(self):
        pkg = Package("", "foobar-2.0-py3-none-any.whl")
        assert pkg.data_dir == "foobar-2.0.data"

    def test_can_read_packages_with_underscore_in_name(self):
        pkg = Package("", "foo_bar-2.0-py3-none-any.whl")
        assert pkg.identifier == "foo-bar==2.0"

    def test_can_read_packages_with_period_in_name(self):
        pkg = Package("", "foo.bar-2.0-py3-none-any.whl")
        assert pkg.identifier == "foo-bar==2.0"


class TestPipRunner(object):
    def test_does_propagate_env_vars(self, pip_factory):
        osutils = CustomEnv({"foo": "bar"})
        pip, runner = pip_factory(osutils)
        wheel = "foobar-1.2-py3-none-any.whl"
        directory = "directory"
        runner.build_wheel(wheel, directory)
        call = pip.calls[0]

        assert "foo" in call.env_vars
        assert call.env_vars["foo"] == "bar"

    def test_build_wheel(self, pip_factory):
        # Test that `pip wheel` is called with the correct params
        pip, runner = pip_factory()
        wheel = "foobar-1.0-py3-none-any.whl"
        directory = "directory"
        runner.build_wheel(wheel, directory)

        assert len(pip.calls) == 1
        call = pip.calls[0]
        assert call.args == ["wheel", "--no-deps", "--wheel-dir", directory, wheel]
        for compile_env_var in pip_no_compile_c_env_vars:
            assert compile_env_var not in call.env_vars
        assert call.shim == ""

    def test_build_wheel_without_c_extensions(self, pip_factory):
        # Test that `pip wheel` is called with the correct params when we
        # call it with compile_c=False. These will differ by platform.
        pip, runner = pip_factory()
        wheel = "foobar-1.0-py3-none-any.whl"
        directory = "directory"
        runner.build_wheel(wheel, directory, compile_c=False)

        assert len(pip.calls) == 1
        call = pip.calls[0]
        assert call.args == ["wheel", "--no-deps", "--wheel-dir", directory, wheel]
        for compile_env_var in pip_no_compile_c_env_vars:
            assert compile_env_var in call.env_vars
        assert call.shim == pip_no_compile_c_shim

    def test_download_all_deps(self, pip_factory):
        # Make sure that `pip download` is called with the correct arguments
        # for getting all sdists.
        pip, runner = pip_factory()
        runner.download_all_dependencies("requirements.txt", "directory")

        assert len(pip.calls) == 1
        call = pip.calls[0]
        assert call.args == ["download", "-r", "requirements.txt", "--dest", "directory", "--exists-action", "i"]
        assert call.env_vars is None
        assert call.shim is None

    def test_download_wheels(self, pip_factory):
        # Make sure that `pip download` is called with the correct arguments
        # for getting lambda compatible wheels.
        pip, runner = pip_factory()
        packages = ["foo", "bar", "baz"]
        runner.download_manylinux_wheels(packages, "directory", "abi")
        expected_prefix = [
            "download",
            "--only-binary=:all:",
            "--no-deps",
            "--platform",
            "manylinux2014_x86_64",
            "--implementation",
            "cp",
            "--abi",
            "abi",
            "--dest",
            "directory",
        ]
        for i, package in enumerate(packages):
            assert pip.calls[i].args == expected_prefix + [package]
            assert pip.calls[i].env_vars is None
            assert pip.calls[i].shim is None

    def test_download_wheels_no_wheels(self, pip_factory):
        pip, runner = pip_factory()
        runner.download_manylinux_wheels([], "directory", "abi")
        assert len(pip.calls) == 0

    def test_does_find_local_directory(self, pip_factory):
        pip, runner = pip_factory()
        pip.add_return((0, (b"Processing ../local-dir\n" b"  Link is a directory," b" ignoring download_dir"), b""))
        runner.download_all_dependencies("requirements.txt", "directory")
        assert len(pip.calls) == 2
        assert pip.calls[1].args == ["wheel", "--no-deps", "--wheel-dir", "directory", "../local-dir"]

    def test_does_find_multiple_local_directories(self, pip_factory):
        pip, runner = pip_factory()
        pip.add_return(
            (
                0,
                (
                    b"Processing ../local-dir-1\n"
                    b"  Link is a directory,"
                    b" ignoring download_dir"
                    b"\nsome pip output...\n"
                    b"Processing ../local-dir-2\n"
                    b"  Link is a directory,"
                    b" ignoring download_dir"
                ),
                b"",
            )
        )
        runner.download_all_dependencies("requirements.txt", "directory")
        assert len(pip.calls) == 3
        assert pip.calls[1].args == ["wheel", "--no-deps", "--wheel-dir", "directory", "../local-dir-1"]
        assert pip.calls[2].args == ["wheel", "--no-deps", "--wheel-dir", "directory", "../local-dir-2"]

    def test_raise_no_such_package_error(self, pip_factory):
        pip, runner = pip_factory()
        pip.add_return((1, b"", (b"Could not find a version that satisfies the " b"requirement BadPackageName ")))
        with pytest.raises(NoSuchPackageError) as einfo:
            runner.download_all_dependencies("requirements.txt", "directory")
        assert str(einfo.value) == ("Could not satisfy the requirement: " "BadPackageName")

    def test_raise_other_unknown_error_during_downloads(self, pip_factory):
        pip, runner = pip_factory()
        pip.add_return((1, b"", b"SomeNetworkingError: Details here."))
        with pytest.raises(PackageDownloadError) as einfo:
            runner.download_all_dependencies("requirements.txt", "directory")
        assert str(einfo.value) == "SomeNetworkingError: Details here."

    def test_inject_unknown_error_if_no_stderr(self, pip_factory):
        pip, runner = pip_factory()
        pip.add_return((1, None, None))
        with pytest.raises(PackageDownloadError) as einfo:
            runner.download_all_dependencies("requirements.txt", "directory")
        assert str(einfo.value) == "Unknown error"


class TestSubprocessPip(TestCase):
    def test_does_use_custom_pip_import_string(self):
        fake_osutils = FakePopenOSUtils([FakePopen(0, "", "")])
        expected_import_statement = "foobarbaz"
        pip = SubprocessPip(osutils=fake_osutils, import_string=expected_import_statement, python_exe=sys.executable)
        pip.main(["--version"])

        pip_execution_string = fake_osutils.popens[0][0][0][2]
        import_statement = pip_execution_string.split(";")[1].strip()
        assert import_statement == expected_import_statement

    def test_check_pip_runner_string_pip(self):
        fake_osutils = FakePopenOSUtils([FakePopen(0, "", "")])
        pip = SubprocessPip(osutils=fake_osutils, python_exe=sys.executable)
        pip.main(["--version"])

        pip_runner_string = fake_osutils.popens[0][0][0][2].split(";")[-1:][0]
        self.assertIn("main", pip_runner_string)
