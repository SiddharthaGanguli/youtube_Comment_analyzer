import logging 
import os
from logging.handlers import RotatingFileHandler

# define the class
class Logger: 
        # define init function by define the file name
    def __init__(self,filename):
        self.filename=filename

    def get_logger(self)-> logging.Logger:
        # create the log directory
        log_dir = "logs"
        os.makedirs(log_dir,exist_ok=True)
        # define the log file path
        log_file=os.path.join(log_dir,self.filename)

        # define logger and set the level
        logger=logging.getLogger(self.filename)
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
            )
            console_handler=logging.StreamHandler()
            console_handler.setFormatter(formatter)

            file_handler=RotatingFileHandler(log_file,maxBytes=5*1024*1024,backupCount=5)
            file_handler.setFormatter(formatter)

            logger.addHandler(console_handler)
            logger.addHandler(file_handler)

        return logger
