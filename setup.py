from setuptools import setup, find_packages

setup(
    name="api-revolver",
    version="0.2.5",
    packages=find_packages(),
    install_requires=["requests"],
    entry_points={
        "console_scripts": [
            "revolver=revolver.cli:main",
        ],
    },
)
