"""Setuptools hooks applying one public-file policy to sdist and wheel builds."""
from pathlib import Path
import sys

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.egg_info import egg_info, manifest_maker

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_policy import allowed_source, require_regular_source


class PublicManifest(manifest_maker):
    def write_manifest(self):
        self.filelist.files = [name for name in self.filelist.files if allowed_source(name, metadata=True)]
        for name in self.filelist.files:
            require_regular_source(name)
        super().write_manifest()


class PublicEggInfo(egg_info):
    def find_sources(self):
        maker = PublicManifest(self.distribution)
        maker.ignore_egg_info_dir = self.ignore_egg_info_in_manifest
        maker.manifest = str(Path(self.egg_info) / 'SOURCES.txt')
        maker.run()
        self.filelist = maker.filelist


class PublicBuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        modules = [item for item in super().find_package_modules(package, package_dir)
                   if allowed_source(item[2])]
        for _, _, filename in modules:
            require_regular_source(filename)
        return modules

    def find_data_files(self, package, src_dir):
        files = [name for name in super().find_data_files(package, src_dir) if allowed_source(name)]
        for name in files:
            require_regular_source(name)
        return files

    def run(self):
        # A reused build directory can otherwise reintroduce a file removed from
        # the current manifest. Refuse it; never delete a developer's files.
        build = Path(self.build_lib)
        if build.exists():
            for path in build.rglob('*'):
                relative = Path('src') / path.relative_to(build)
                if path.is_symlink() or (path.is_file() and not allowed_source(relative.as_posix())):
                    raise ValueError(f'Unsafe stale build output; use a clean build directory: {relative}')
        super().run()


setup(cmdclass={'egg_info': PublicEggInfo, 'build_py': PublicBuildPy})
