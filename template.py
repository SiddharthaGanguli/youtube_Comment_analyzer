import os 
from pathlib import Path
from logging_.logger_ import Logger

logger = Logger("template.log").get_logger()

# define project
project='Sentiment_analysis'

list_of_files = [ 
    f'src/{project}/__init__.py',

    f'src/{project}/entity/__init__.py',
    f'src/{project}/entity/entity.py',

    f'src/{project}/config/__init__.py',
    f'src/{project}/config/config.py',

    f'src/{project}/components/__init__.py',
    f'src/{project}/components/data_ingestion.py',
    f'src/{project}/components/data_validation.py',
    f'src/{project}/components/data_preprocessing.py',
    f'src/{project}/components/model_training.py',
    f'src/{project}/components/model_evaluation.py',


    f'src/{project}/pipeline/__init__.py',
    f'src/{project}/pipeline/pipeline_01_data_ingestion.py',
    f'src/{project}/pipeline/pipeline_02_data_validation.py',
    f'src/{project}/pipeline/pipeline_03_data_preprocessing.py',
    f'src/{project}/pipeline/pipeline_04_model_training.py',
    f'src/{project}/pipeline/pipeline_05_model_evaluation.py',

    f'src/{project}/utils/__init__.py',
    f'src/{project}/utils/common.py',

    "notebooks/.gitkeep",

    'logging_/logger_.py',
    'config/config.yaml',
    'config/params.yaml',

    'data/raw/.gitkeep',
    'data/processed/.gitkeep',

    'main.py',
    'setup.py',
    'requirements.txt',
    'README.md',

    ".github/workflows/ci.yaml"



]
for filepath in list_of_files:
    filepath=Path(filepath)
    file_dir,file_name=os.path.split(filepath)

    if file_dir:
        os.makedirs(file_dir,exist_ok=True)
        logger.info(f"Directory ready: {file_dir}")

    if not filepath.exists():
        filepath.touch()
        logger.info(f"Created file: {filepath}")

logger.info("Project template created successfully.")