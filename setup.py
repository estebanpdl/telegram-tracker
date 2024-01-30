from setuptools import setup, find_packages

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(
    name='telegram-tracker',
    version='1.1',
    packages=find_packages(),
    install_requires=requirements,
    entry_points={
        'console_scripts': [
            'build-dataset=telegram_tracker.build_datasets:main',
            'channels-to-network=telegram_tracker.channels_to_network:main',
            'telegram-tracker=telegram_tracker.telegram_tracker:main',
        ],
    },
)
