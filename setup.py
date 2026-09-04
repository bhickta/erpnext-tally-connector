from setuptools import setup, find_packages

with open('requirements.txt') as f:
	install_requires = f.read().strip().split('\n')

# get version from __version__ variable in express_tally/__init__.py
from express_tally import __version__ as version

setup(
	name='express_tally',
	version=version,
	description='Reusable bidirectional ERPNext and Tally integration framework',
	author='Laxman Tandon',
	author_email='laxmantandon@gmail.com',
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
