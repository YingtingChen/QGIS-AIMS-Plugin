################################################################################
#
# Copyright 2015 Crown copyright (c)
# Land Information New Zealand and the New Zealand Government.
# All rights reserved
#
# This program is released under the terms of the 3 clause BSD license. See the 
# LICENSE file for more information.
#
################################################################################

import threading

notify_lock = threading.RLock()
sync_lock = threading.RLock()

class Observable(threading.Thread):

    def __init__(self): 
        super(Observable,self).__init__()       
        #threading.Thread.__init__(self)
        self._stop = threading.Event()
        self._observers = []

    def register(self, observer):
        self._observers.append(observer)
        
    def deregister(self,observer):
        if observer in self._observers: self._observers.remove(observer)
    
    def notify(self, *args, **kwargs):
        '''Notify all registered listeners'''
        for observer in self._observers:
            with notify_lock:
                observer.observe(*args, **kwargs)
                
    def observe(self, *args, **kwargs):
        '''listen method called by notification, default calls in turm call notify but override this as needed'''
        if not self.stopped():
            self.notify(*args, **kwargs)
    
    #promoted to prevent notifications on stopped threads    
    def stop(self):
        self._stop.set()

    def stopped(self):
        if not hasattr(self, '_stop'):
            pass
        return self._stop.isSet()