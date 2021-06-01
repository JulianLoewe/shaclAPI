import time, logging

logger = logging.getLogger(__name__)

class ValidationResultTransmitter():
    """
    Class used to transmit validation results from a backend to the api.
    This can be done via an endpoint or using a multiprocessing.Queue.
    """

    def __init__(self, output_queue=None, first_val_time_queue=None):
        self.output_queue = output_queue
        self.timestamp_of_first_result_send = False
        self.first_val_time_queue = first_val_time_queue

    def send(self, instance, shape, valid, reason):
        logger.debug({'instance': instance, 
                        'validation': (shape, valid, reason)})
        if not self.timestamp_of_first_result_send and self.first_val_time_queue:
            self.timestamp_of_first_result_send = True
            self.first_val_time_queue.put({"topic": "first_validation_result", "time": time.time()})

        if self.output_queue:
            self.output_queue.put({'instance': instance, 
                        'validation': (shape, valid, reason)})
        else:
            pass
    
    def use_streaming(self):
        return (self.output_queue != None)
        