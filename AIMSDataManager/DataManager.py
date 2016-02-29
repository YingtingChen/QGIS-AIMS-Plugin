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

import Queue
import pickle
import copy
import time
import pprint
import collections
from Address import Address, AddressChange, AddressResolution,Position
from AddressFactory import AddressFactory
#from DataUpdater import DataUpdater
from DataSync import DataSync,DataSyncFeatures,DataSyncChangeFeed,DataSyncResolutionFeed
from datetime import datetime as DT
from AimsUtility import ActionType,ApprovalType,FeedType,readConf
from AimsLogging import Logger

LOCALADL = 'aimsdata'
LOCALDB = 'aimsdata.sb'
UPDATE_INTERVAL = 5#s
LOGFILE = 'admlog'
SW = (174.75918,-41.29515)
NE = (174.78509,-41.27491)
FEATURES_THREAD_TIMEOUT = 10


aimslog = None



class DataManager(object):
    '''Initialises maintenance thread and provides queue accessors'''
    # ADL - Address-Data List
    # {'features':{adr1_id:Address1, adr2_id:Address2...},'changefeed':{},'resolutionfeed':{}} 
    #ADL = {FeedType.FEATURES:{},FeedType.CHANGEFEED:{},FeedType.RESOLUTIONFEED:{}} 
    # conf = feat:(count,index),cfeed:(count,page,index),rfeed:(count,page,index)
    #conf = {}
    global aimslog
    aimslog = Logger.setup()
    
    
    def __init__(self):
        #self.ioq = {'in':Queue.Queue(),'out':Queue.Queue()}        
        self.persist = Persistence()
        self.conf = readConf()
        self._initDS()
        
    def _initDS(self):
        '''initialise the data sync queues/threads'''
        self.ioq = {ft:None for ft in FeedType.reverse}
        self.ds = {ft:None for ft in FeedType.reverse}
        self.stamp = {ft:time.time() for ft in FeedType.reverse}
        
        #init the three different feed threads
        self.dsr = {
            FeedType.FEATURES:DataSyncFeatures,
            FeedType.CHANGEFEED:DataSyncChangeFeed,
            FeedType.RESOLUTIONFEED:DataSyncResolutionFeed
        }
        for ref in self.dsr:
            self._initFeedDS(ref,self.dsr[ref])


        
    def _initFeedDS(self,ft,feedclass): 
        ts = '{0:%y%m%d.%H%M%S}'.format(DT.now())
        params = ('ReqADU.{}.{}'.format(ft,ts),ft,self.persist.tracker[ft],self.conf)
        self.ioq[ft] = {n:Queue.Queue() for n in ('in','out','resp')}
        self.ds[ft] = feedclass(params,self.ioq[ft])
        self.ds[ft].setup(self.persist.coords['sw'],self.persist.coords['ne'])
        self.ds[ft].setDaemon(True)
        self.ds[ft].start()
        
    def close(self):
        '''shutdown closing/stopping ds threads and persisting data'''
        for ds in self.ds.values():
            #ds.close()
            #ds.stop()
            pass
        self.persist.write()
        
    def _restart(self):
        '''If a DataSync thread crashes restart it'''
        for ft in FeedType.reverse:
            if not self.ds[ft].isAlive():
                aimslog.warn('DS thread {} has died, restarting'.format(ft))
                del self.ds[ft]
                self._initFeedDS(ref,self.dsr[ref])
            
        
    #Client Access
    def setbb(self,sw=None,ne=None):
        '''Resetting the bounding box triggers a complete refresh of the features address data'''
        #TODO add move-threshold to prevent small moves triggering an update
        if self.persist.coords['sw'] != sw or self.persist.coords['ne'] != ne:
            #throw out the current features addresses
            self.persist.ADL[FeedType.FEATURES] = self.persist._initADL()[FeedType.FEATURES]
            #save the new coordinates
            self.persist.coords['sw'],self.persist.coords['ne'] = sw,ne
            #kill the old features thread
            if self.ds[FeedType.FEATURES].isAlive():
                aimslog.info('Attempting Features Thread STOP')
                self.ds[FeedType.FEATURES].stop()
                self.ds[FeedType.FEATURES].join(FEATURES_THREAD_TIMEOUT)
            #TODO investigate thread non-stopping issues
            if self.ds[FeedType.FEATURES].isAlive(): aimslog.warn('Features Thread JOIN timeout')
            del self.ds[FeedType.FEATURES]
            #reinitialise a new features DataSync
            self._initFeedDS(FeedType.FEATURES,DataSyncFeatures)

        
    #Push and Pull relate to features feed actions
    def push(self,newds):
        pass
        #return self._scan(newds)
        
    def pull(self):
        '''Return copy of the ADL. Speedup, insist on deepcopy at address level'''
        return self.persist.ADL
    
    def refresh(self):
        '''returns feed length counts without client having to do a pull/deepcopy'''
        self._restart()
        self._monitor()
        return [(self.stamp[f],len(self.persist.ADL[f])) for f in FeedType.reverse]
        
    
    def action(self,at,address):
        '''Some user initiated approval action'''
        action = {at:[address,]}
        self.ioq[FeedType.RESOLUTIONFEED]['in'].put(action)
        
        
    def _monitor(self):
        '''for each feed check the out queue and put any new items into the ADL'''
        for ft in FeedType.reverse:
            while not self.ioq[ft]['out'].empty():
                #because the queue isnt populated till all pages are loaded we can just swap out the ADL
                self.persist.ADL[ft] = self.ioq[ft]['out'].get()
                self.stamp[ft] = time.time()

        #self.persist.write()
        return self.persist.ADL
    
    def response(self,ft=FeedType.CHANGEFEED):
        resp = ()
        while not self.ioq[ft]['resp'].empty():
            resp += (self.ioq[ft]['resp'].get(),)
        return resp
        
        
    def _scan(self,ds):
        '''compare provided and current ADL. Split out deletes/adds/updates'''
        #self._synchroniseChangeFeed(ds)
        #self._synchroniseResolutionFeed(ds)
        self._scanChangeFeedChanges(ds[FeedType.CHANGEFEED])
        self._scanResolutionFeedChanges(ds[FeedType.RESOLUTIONFEED])
      
    #convenience methods  
    def addAddress(self,address):
        address.setChangeType(ActionType.reverse[ActionType.ADD].title())
        self.ioq[FeedType.CHANGEFEED]['in'].put({ActionType.ADD:(address,)})        
    
    def retireAddress(self,address):
        address.setChangeType(ActionType.reverse[ActionType.RETIRE].title())
        self.ioq[FeedType.CHANGEFEED]['in'].put({ActionType.RETIRE:(address,)})
    
    def updateAddress(self,address):
        address.setChangeType(ActionType.reverse[ActionType.UPDATE].title())
        self.ioq[FeedType.CHANGEFEED]['in'].put({ActionType.UPDATE:(address,)})    
        
    #----------------------------
    def acceptAddress(self,address):
        address.setQueueStatus(ApprovalType.revalt[ApprovalType.ACCEPT].title())
        self.ioq[FeedType.RESOLUTIONFEED]['in'].put({ApprovalType.ACCEPT:(address,)})        
    
    def declineAddress(self,address):
        address.setQueueStatus(ApprovalType.revalt[ApprovalType.DECLINE].title())
        self.ioq[FeedType.RESOLUTIONFEED]['in'].put({ApprovalType.DECLINE:(address,)})
    
    def repairAddress(self,address):
        address.setQueueStatus(ApprovalType.revalt[ApprovalType.UPDATE].title())
        self.ioq[FeedType.RESOLUTIONFEED]['in'].put({ApprovalType.UPDATE:(address,)})
        

    #CM
        
    def __enter__(self):
        return self
    
    def __exit__(self,exc_type=None, exc_val=None, exc_tb=None):
        return self.close()

        

class Persistence():
    '''static class for persisting config/long-lived information'''
    
    tracker,coords,ADL = 3*(None,)
    
    def __init__(self):

        if not self.read():
            self.ADL = self._initADL() 
            self.coords = {'sw':SW,'ne':NE}
            #default tracker, gets overwritten
            self.tracker = {ft:{'page':[1,1],'index':1,'threads':1,'interval':5} for ft in FeedType.reverse}
            self.write() 
            
    def _initADL(self):
        '''Read ADL from serial and update from API'''
        return {FeedType.FEATURES:[],FeedType.CHANGEFEED:[],FeedType.RESOLUTIONFEED:[]}
    
    #Disk Access
    def read(self,localds=LOCALADL):
        '''unpickle local store'''  
        try:
            archive = pickle.load(open(localds,'rb'))
            self.tracker,self.coords,self.ADL = archive
        except:
            return False
        return True
    
    def write(self, localds=LOCALADL):
        try:
            archive = [self.tracker,self.coords,self.ADL]
            pickle.dump(archive, open(localds,'wb'))
        except:
            return False
        return True

testdata = []
def test():
    af = {ft:AddressFactory.getInstance(ft) for ft in FeedType.reverse}

    with DataManager() as dm:
        test1(dm,af)
        
        
def test1(dm,af):
    print 'start'
    
    #dm.persist.ADL = testdata
    #get some data
    dm.refresh()
    listofaddresses = dm.pull()
    
    #TEST SHIFT
    testfeatureshift(dm)
    
    # TEST CF
    testchangefeedAUR(dm,af[FeedType.FEATURES])
    
    # TEST RF
    testresolutionfeedAUD(dm)

    
    aimslog.info('*** Resolution ADD '+str(time.clock()))    
    
    print 'entering response mode'
    while True:
        aimslog.info('*** Main TICK '+str(time.clock()))
        testresp(dm)
        time.sleep(5)
        
def testfeatureshift(dm):
    time.sleep(10)
    aimslog.info('*** Main SHIFT '+str(time.clock()))
    dm.setbb(sw=(174.76918,-41.28515), ne=(174.79509,-41.26491))
    time.sleep(30)
    testresp(dm)
    time.sleep(5)
    
    
def testchangefeedAUR(dm,ff):
    addr_c = gettestdata(ff)
    
    aimslog.info('*** Change ADD '+str(time.clock()))
    #addr_c.setChangeType(ActionType.reverse[ActionType.ADD])
    #listofaddresses[FeedType.CHANGEFEED].append(addr_c)
    #dm.push(listofaddresses)
    dm.addAddress(addr_c)
    time.sleep(10)
    r = testresp(dm)
    d = dm.pull()
    time.sleep(5)    
    
    aimslog.info('*** Change UPDATE '+str(time.clock()))
    #addr_c.setChangeType(ActionType.reverse[ActionType.ADD])
    #listofaddresses[FeedType.CHANGEFEED].append(addr_c)
    #dm.push(listofaddresses)
    addr_c.setFullAddress('Unit B, 16 Islay Street, Glenorchy')
    dm.updateAddress(addr_c)
    time.sleep(10)
    r = testresp(dm)
    d = dm.pull()
    time.sleep(5)    
    
    
    aimslog.info('*** Change RETIRE '+str(time.clock()))
    #addr_c.setChangeType(ActionType.reverse[ActionType.ADD])
    #listofaddresses[FeedType.CHANGEFEED].append(addr_c)
    #dm.push(listofaddresses)
    dm.retireAddress(addr_c)
    time.sleep(10)
    r = testresp(dm)
    d = dm.pull()
    time.sleep(5)
    
def testresolutionfeedAUD(dm):
    addr_r = f3[0].getAddress('resolution_accept')


def testresp(dm):
    r = None
    aimslog.info('*** Main COUNT {}'.format(dm.refresh()))  
    out = dm.pull()
    for o in out:
        #aimslog.info('*** Main OUTPUT {} - [{}]'.format(out[o],len(out[o])))
        aimslog.info('*** Main OUTPUT {} [{}]'.format(o,len(out[o])))
    
    resp = dm.response()
    for r in resp:
        #aimslog.info('*** Main RESP {} - [{}]'.format(r,len(resp))) 
        aimslog.info('*** Main RESP {} [{}]'.format(r,len(resp)))
        
    return r
            
def gettestdata(ff):
    a = ff.getAddress('change_add')
    p = Position.getInstance(
        {'position':{'type':'Point','coordinates': [168.38392191667,-44.8511013],'crs':{'type':'name','properties':{'name':'urn:ogc:def:crs:EPSG::4167'}}},'positionType':'Centroid','primary':True}
    )
    a.setAddressType('Road')
    a.setAddressNumber('16')
    a.setAddressId('29')
    a.setLifecycle('Current')
    a.setRoadCentrelineId('11849')
    a.setRoadName('Islay')
    a.setRoadType('Street'),
    a.setSuburbLocality('Glenorchy')
    a.setFullAddressNumber('16')
    a.setFullRoadName('Islay Street')
    a.setFullAddress('16 Islay Street, Glenorchy')
    a._addressedObject_addressableObjectId = '1416143'
    a.setObjectType('Parcel')
    
    a.setUnitType('Unit')
    a.setUnitValue('b')

    a.setAddressPosition(p)

    a._codes_suburbLocalityId = '2104'
    a._codes_parcelId = '3132748'
    a._codes_meshblock = '3174100'
    return a

    

            
if __name__ == '__main__':
    test()  