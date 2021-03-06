#!/usr/bin/python

# Copyright (C) 2012 University of Southern California
# This software is licensed under the GPLv3 license, included in
# ./GPLv3-LICENSE.txt in the source distribution

import base64
import cStringIO
import datetime
import errno
import glob
import json
import logging
from os.path import basename
import shutil
import signal
from subprocess import Popen, PIPE
import sys
import tarfile
import tempfile
import threading
import time
import traceback

from magi.daemon.externalAgentsThread import ExternalAgentsThread, PipeTuple
from magi.messaging.api import *
import magi.modules
import magi.modules.dataman
from magi.util import config, helpers, database
from magi.util.agent import agentmethod
from magi.util.calls import doMessageAction
from magi.util.software import requireSoftware


log = logging.getLogger(__name__)

class Daemon(threading.Thread):
	"""
		The daemon process that listens to the messaging system and dispatches messages according
		to their destination dock.  It will also handle messages for the dock 'daemon', these include 
		messages such as 'exec'.
	"""

	def __init__(self, hostname, transports):
		threading.Thread.__init__(self, name='daemon')
		# 9/16/2013 hostname is passed in from the magi_daemon script correctly 
		self.hostname = hostname
		self.messaging = Messenger(self.hostname)
		#self.messaging.startDaemon()
		
		self.staticAgents = list()  # statically loaded thread agents
		self.threadAgents = list()  # dynamically loaded thread agents
		self.pAgentPids = dict() # process agent's process ids
		
		#starting the external/process agents control thread
		self.extAgentsThread = ExternalAgentsThread(self.messaging)
		self.extAgentsThread.start()

		if database.isDBEnabled():
			try:
				log.info("Starting Data Manager Agent")
				self.startAgent(code=magi.modules.dataman.__path__[0], 
							    name="dataman", dock="dataman", static=True)
				
				#Inserting the topology information in the database
				topo_collection = database.getCollection(agentName="topo_agent")
				topoGraph = config.getTopoGraph()
				topo_collection.insert({'nodes' : json.dumps(topoGraph.nodes()),
										'edges' : json.dumps(topoGraph.edges())})
				
			except Exception, e:
				log.exception("Exception while trying to run the data manager agent.")
				raise e
			
		self.configureMessaging(self.messaging, transports)
		
		self.done = False

	def configureMessaging(self, messaging, transports, **kwargs):
		"""
			Called by main process to setup the local messaging system with the necessary links.
		"""
		one = False
		for entry in transports:
			try: 
				clazz = entry.pop('class')
				conn = globals()[clazz](**entry)  # assumes we have imported from messaging.api.*
				messaging.addTransport(conn, True)
				one = True
			except Exception, e:
				log.error("Failed to add new transport %s: %s", entry, e)

		if not one:
			# Couldn't make any links, might as well just quit
			raise IOError("Unable to start any transport links, I am stranded")

	def run(self):
#		import cProfile
#		cProfile.runctx('self.runX()', globals(), locals(), '/tmp/cprofile_daemon')
#	
#	def runX(self):
		"""
			Daemon thread loop. Continual processing of incoming messages 
			while monitoring for the stop flag.
		"""
		self.threadId = helpers.getThreadId()
		log.info("Daemon started. Thread id: " + str(self.threadId))
		
		try:
			for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, 
					    signal.SIGQUIT]:
				signal.signal(sig, self.signalhandler)
		except:
			pass
		
		while not self.done:
			try:
				try:
					#infinitely blocked calls will not respond to signals also
					msg = self.messaging.nextMessage(block=True, timeout=1)
				except Queue.Empty:
					continue
				
				#if msg is None:
				#	continue
				# No longer need PoisinPill to break out of the nextMessage loop as we are using a timeout
				#if type(msg) is str and msg == 'PoisinPill': # don't cause conversion to string for every message
				#	break

				progress = False
				for dock in msg.dstdocks:
					if dock == 'daemon':
						log.log(5, "Handling message for dock daemon with local call")
						progress = True
						doMessageAction(self, msg, self.messaging)
		
					for tAgent in self.staticAgents + self.threadAgents:
						if dock in tAgent.docklist:
							log.log(5, "Handing message for dock %s off to threaded agent %s", dock, tAgent)
							progress = True
							tAgent.rxqueue.put(msg)
		
					if self.extAgentsThread.wantsDock(dock):
						log.log(5, "Handing message for dock %s off to external agents thread", dock)
						progress = True
						self.extAgentsThread.fromNetwork.put(msg)

					if not progress:
						log.debug("Unknown dock %s, nobody processed.", dock)

			except Exception, e:
				log.error("Problems in message distribution: %s", e, exc_info=1)
				time.sleep(0.5)
				
		log.info("Daemon stopped.")


	def signalhandler(self, signum, frame):
		print "Handling kill signal. Shutting down."
		log.info("Handling kill signal")
		self.stop(None)

	
	@agentmethod()
	def stop(self, msg=None, unloadAgents=True):
		"""
			Called to shutdown the daemon nicely by stopping all agent threads, stopping external processes
			and stopping the messaging thread.
		"""
		log.info("Stopping daemon gracefully")
		
		if unloadAgents:
			#unload all agents
			self.unloadAll(msg, unLoadStaticAgents=True)
			
		log.info("Stopping process agent loop")
		self.extAgentsThread.stop(unloadAgents)
		log.info("Joining with process agent loop")
		self.extAgentsThread.join(1.0)  # try and be nice and wait, otherwise just move along
		
		log.info("stopping messaging")
		self.messaging.stop()
		log.info("messaging stopped")
		
		self.done = True
		
		#self.messaging.poisinPill() #without this daemon could block on nextMessage
		log.info("daemon stop complete")
		
		return self.done

		
	@agentmethod()
	def loadAgent(self, msg, code, name, dock, tardata=None, path=None, execargs=None, idl=None):
		"""
			Primary use daemon method call to start agents as a thread or process
		"""
		# Safety check, don't overload dock from loadAgent
		for tAgent in self.staticAgents + self.threadAgents:
			if dock in tAgent.docklist:
				log.info("Agent %s already loaded on dock %s. Returning successful \"load\".", tAgent, dock)
				# Send complete anyhow, perhaps a flag to indicate already loaded, but don't stop event flow process
				# 9/14: changed testbed.nodename to self.hostname to support desktop daemos 
				self.messaging.trigger(event='AgentLoadDone', agent=name, nodes=[self.hostname])
				return

		if self.extAgentsThread.wantsDock(dock):
			log.info("Agent already loaded on dock %s. Returning successful \"load\".", dock)
			# 9/14: changed testbed.nodename to self.hostname to support desktop daemons 
			self.messaging.trigger(event='AgentLoadDone', agent=name, nodes=[self.hostname])
			return
		
		# Start by extracting the tardata into the appropriate modules directory if provided

		# TODO: 5/28/2013 
		# code is the location of the directory where the agent module resides
		# Current the orch generates the code variable by concatenating agentname and word "code"
		# However there are no checks, these should be present in the orch or here? 
		# if code is not specified, the tardata or path needs to be specified 
		# If code is not specified, then find out the code name from the idl. 
		#
		cachepath = os.path.join(magi.modules.__path__[0], os.path.basename(os.path.normpath(code)))
		
		# Different instances of MAGI daemon share the file system in desktop mode
		# Differentiating module folders by concatenating MAGI daemon's hostname
		from magi.testbed import testbed
		from magi.testbed.desktop import DesktopExperiment
		if isinstance(testbed.getTestbedClassInstance(), DesktopExperiment):
			cachepath = cachepath + "_" + self.hostname
		
		if tardata is not None:
			self.extractTarBuffer(cachepath, tardata)
		elif path is not None:
			self.extractTarPath(cachepath, path)
		elif os.path.exists(code):
			self.extractTarPath(cachepath, code)
		elif os.path.exists(cachepath):
			#In case agent is already cached
			pass
		else:
			raise OSError(errno.ENOENT, "Invalid path to agent implementation: %s" % code)

		return self.startAgent(cachepath, name, dock, execargs, idl)
		
	
	@agentmethod()
	def unloadAgent(self, msg, name):
		"""
			Unload the named agent, if it's loaded. 
		"""
		unloaded = []
		
		call = {'version': 1.0, 'method': 'stop', 'args': {}}
		stop_msg = MAGIMessage(contenttype=MAGIMessage.YAML, data=yaml.safe_dump(call))
		
		for i in range(len(self.threadAgents)):
			if name == self.threadAgents[i].agentname:
				log.debug("Unloading agentname %s dock %s",self.threadAgents[i].agentname, self.threadAgents[i].docklist)
				self.threadAgents[i].rxqueue.put(stop_msg)
				self.threadAgents[i].join(0.5)
				# 9/14 Changed testbed.nodename to self.hostname to support desktop daemons  
				# 2/21 Moved to the agent code
				# self.messaging.trigger(event='AgentUnloadDone', agent=name, nodes=[self.hostname])
				unloaded.append(i)
		
		if len(unloaded):
			self.threadAgents[:] = [a for a in self.threadAgents if a.agentname != name]

		# now check for process agents. If we find a dock, send the unload message to the 
		# agent. If it is well behaving, it'll commit harikari after cleaning up its
		# resources. 
		if not len(unloaded):
			data = yaml.load(msg.data)
			log.debug('message data: %s (%s)', data, type(data))
			if not 'args' in data or not 'dock' in data['args']:
				log.warning('No dock given in agentUnload. I do not know how to contact the'
							' process agent to tell it to unload. Malformed or incomplete '
							'message for AgentUnload')
			else:
				dock = data['args']['dock']
				if not self.extAgentsThread.wantsDock(dock):
					log.warning('unloadAgent for dock I know nothing about. Ignoring.')
				else:
					log.debug('Sending stop message to process agent.')
					call = {'version': 1.0, 'method': 'stop', 'args': {}}
					stop_msg = MAGIMessage(docks=dock, contenttype=MAGIMessage.YAML, 
										   data=yaml.safe_dump(call))
					self.extAgentsThread.fromNetwork.put(stop_msg)

					# TODO: remove dock and cleanup the external agent data structures.
					#		or confirm that external agents thread correctly discovers
					#		the transport is down and cleans things up correctly. 
					try:
						del self.pAgentPids[name]
					except KeyError:
						log.error("process agent process id not available")
						pass
		
		return True
	
	
	@agentmethod()
	def unloadAll(self, msg=None, unLoadStaticAgents=False):
		"""
			Call to unload all agents, generally used for testing
		"""
		call = {'version': 1.0, 'method': 'stop', 'args': {}}
		stop_msg = MAGIMessage(contenttype=MAGIMessage.YAML, data=yaml.safe_dump(call))
		
		log.info("Unloading all threaded agents")			
		for tAgent in self.threadAgents:
			log.debug("Stopping %s", tAgent)
			tAgent.rxqueue.put(stop_msg)
		
		if unLoadStaticAgents:
			log.info("Unloading all static agents")	
			for sAgent in self.staticAgents:
				log.debug("Stopping %s", sAgent)
				sAgent.rxqueue.put(stop_msg)
			
		for tAgent in self.threadAgents:
			tAgent.join(0.5) # try but don't wait around forever
			
		if unLoadStaticAgents:
			for sAgent in self.staticAgents:
				sAgent.join()
			
		#stop process agents as well
		self.extAgentsThread.unloadAll()

	
	@agentmethod()
	def joinGroup(self, msg, group, nodes):
		"""
			Request to join a particular group
		"""
		if self.hostname in nodes:
			self.messaging.join(group, "daemon")
			# 9/14: Changed testbed.nodename to self.hostname to support desktop daemons  
			self.messaging.trigger(event='GroupBuildDone', group=group, nodes=[self.hostname])

	@agentmethod()
	def leaveGroup(self, msg, group, nodes):
		""""
			Request to leave a particular group
		"""
		if self.hostname in nodes:
			self.messaging.leave(group, "daemon")
			# 9/14: Changed testbed.nodename to self.hostname 
			# to support desktop daemons  
			self.messaging.trigger(event='GroupTeardownDone', 
								   group=group, nodes=[self.hostname])
	
	
	@agentmethod()
	def groupPing(self, msg, group):
		"""
			Method to check if messages sent to a group are reaching the node
			This helps check if the groups have been built successfully
			When a node joins/leaves a group, it may take a while for the 
			information to propagate throughout the network
		"""
		self.messaging.trigger(event='GroupPong', 
							   group=group, nodes=[self.hostname])
	
	@agentmethod()
	def ping(self, msg):
		"""
			Alive like method call that will send a pong back to the caller
		"""
		res = {
		        'pong': True
		}
		# Added a data part to the message otherwise it gets dropped 
		# by the local daemon itself 
		self.messaging.send(MAGIMessage(nodes=msg.src, docks='pong', 
									    contenttype=MAGIMessage.YAML, 
									    data=yaml.safe_dump(res)))
	
	
	@agentmethod()
	def getStatus(self, msg, groupMembership=False, agentInfo=False):
		"""
			gives the group membership and agent information: 
			pid, agentname, threadId
        """ 
		functionName = self.getStatus.__name__
		helpers.entrylog(log, functionName, locals())
		result = dict()
		result['status'] = True
		
		if groupMembership:
			groupMembership = dict(self.messaging.groupMembership)
			result['groupMembership'] = groupMembership
			
		if agentInfo:
			agentInfo = []
			processId = os.getpid()
			for tAgent in self.staticAgents + self.threadAgents:
				agentInfo.append({"name": tAgent.agentname, 
								  "processId": processId, 
								  "threadId": tAgent.tid})
			for name in self.pAgentPids.keys():
				agentInfo.append({"name": name, 
								  "processId": self.pAgentPids[name]})
			result['agentInfo'] = agentInfo
		
		self.messaging.send(MAGIMessage(nodes=msg.src, docks=msg.srcdock, 
									    contenttype=MAGIMessage.YAML, 
									    data=yaml.safe_dump(result)))	
		helpers.exitlog(log, functionName)
		
	@agentmethod()
	def getLogsArchive(self, msg):
		""" 
            Tars the log directory and sends it to the requester as a message" 
        """ 
		functionName = self.archive.__name__
		helpers.entrylog(log, functionName, locals())
		logDir = config.getLogDir()
		store = cStringIO.StringIO()
		logTar = tarfile.open(fileobj=store, mode='w:gz')
		logTar.add(logDir, arcname=os.path.basename(logDir))
		logTar.close()
		result = base64.encodestring(store.getvalue())
		self.messaging.send(MAGIMessage(nodes=msg.src, docks=msg.srcdock, 
									    contenttype=MAGIMessage.YAML, 
									    data=yaml.safe_dump(result)))
		helpers.exitlog(log, functionName)
	
	@agentmethod()
	def archive(self, msg, destinationDir=config.getTempDir()):
		""" 
            Tars the log directory" 
        """ 
		functionName = self.archive.__name__
		helpers.entrylog(log, functionName, locals())
		logDir = config.getLogDir()
		logTar = tarfile.open(name=os.path.join(destinationDir, 
								"logs_%s.tar.gz"%(datetime.datetime.now()
												.strftime("%Y%m%d_%H%M%S"))), 
							  mode='w:gz')
		logTar.add(logDir, arcname=os.path.basename(logDir))
		logTar.close()
		helpers.exitlog(log, functionName)
		
	@agentmethod()
	def reboot(self, msg, distributionDir=None, noUpdate=False, noInstall=False, expConf=None, nodeConf=None):
		"""
		    reinvokes magi_bootstrap, the boostrap script invokes stop() and does a clean shutdown and then 
		    restarts 
		"""
		functionName = self.reboot.__name__
		helpers.entrylog(log, functionName, locals())
		
		if not distributionDir:
			distributionDir = config.getDistDir()
		rebootCmd = "sudo %s/magi_bootstrap.py -p %s" %(distributionDir, distributionDir)
		if noUpdate:
			rebootCmd += ' --noupdate'
		if noInstall:
			rebootCmd += ' --noinstall'
		
		if not expConf and not nodeConf:
			nodeConf = config.getNodeConfFile()
				
		if expConf:
			rebootCmd += ' --expconf %s' %(expConf)
		if nodeConf:
			rebootCmd += ' --nodeconf %s' %(nodeConf)
		
		log.info("Rebooting: %s" %(rebootCmd))
		
		self.messaging.send(MAGIMessage(nodes=msg.src, 
										docks=msg.srcdock, 
										contenttype=MAGIMessage.YAML, 
										data=yaml.safe_dump({'status' : True})))
		
		Popen(rebootCmd.split())
		
		helpers.exitlog(log, functionName)	


	# Internal functions

	def startAgent(self, code=None, name=None, dock=None, execargs=None, idl=None, static=False):
		"""
			Internal function to invoke an agent
		"""
		# Now find the interface definition and load it
		try:
			log.debug('startAgent code: %s, idl: %s' % (code, idl))
			dirname = code
			if idl:
				idlFile = dirname+'/%s.idl' % idl
			else:
				idlFile = glob.glob(dirname+'/*.idl')[0]
		except IndexError:
			log.debug("No valid interface file in %s", dirname) 
			raise OSError(errno.ENOENT, "No valid interface file found in %s" % dirname)

		log.debug('reading interface file %s...' % idlFile)

		fp = open(idlFile)
		interface = fp.read()
		fp.close()
		interface = yaml.load(interface)

		# If there are software dependencies, load them before loading the agent
		if 'software' in interface:
			for package in interface['software']:
				log.info('Loading required package %s for agent %s.', package, name)
				requireSoftware(package)
				
		compileCmd = interface.get('compileCmd')
		if compileCmd:
			log.info("Running specified compilation command: '%s' under directory '%s'" %(compileCmd, dirname))
			p = Popen(compileCmd.split(), cwd=dirname, stdout=PIPE, stderr=PIPE)
			if p.wait():
				raise OSError("Exception while running the specified compilation command. %s", p.communicate())

		# Based on the interface execution method, execute the agent
		execstyle = interface['execute']
		mainfile = os.path.join(dirname, interface['mainfile'])

		log.info('Running agent from file %s' % mainfile)
		
		# GTL TODO: handle exceptions from threaded agents by removing
		# the agents and freeing up the dock(s) for the agent.
		try:
			if execstyle == 'thread':
				# A agent should know the hostname and its own name  
				from magi.daemon.threadInterface import ThreadedAgent
				agent = ThreadedAgent(self.hostname, name, mainfile, dock, execargs, self.messaging)
				agent.start()
				log.info("Started threaded agent %s", agent)
				if static:
					self.staticAgents.append(agent)
				else:
					self.threadAgents.append(agent)
				#2/13/14 Moved it to threadInterface
				#self.messaging.trigger(event='AgentLoadDone', agent=name, nodes=[self.hostname])
				
			else:
				# Process agent, use the file as written to disk
				if (not execargs) or (type(execargs) != dict):
					execargs = dict()
					
				# Process agent need to know hostname 
				execargs['hostname'] = self.hostname
				
				# I apologize for this abuse
				args = ['%s=%s' % (str(k), yaml.dump(v)) for k,v in execargs.iteritems()]
				
				os.chmod(mainfile, 00777)
				stderrname = os.path.join(config.getLogDir(), name + '.stderr')
				stderr = open(stderrname, 'w')		# GTL should this be closed? If so, when?
				log.info("Starting %s, stderr sent to %s", name, stderrname)
				
				if execstyle == 'pipe':
					args.append('execute=pipe')
					cmdList = [mainfile, name, dock, config.getNodeConfFile(), config.getExperimentConfFile()] + args
					log.info('running: %s', ' '.join(cmdList))
					agent = Popen(cmdList, close_fds=True, stdin=PIPE, stdout=PIPE, stderr=stderr)
					self.extAgentsThread.fromNetwork.put(PipeTuple([dock], InputPipe(fileobj=agent.stdout), OutputPipe(fileobj=agent.stdin)))
	
				elif execstyle == 'socket':
					args.append('execute=socket')
					cmdList = [mainfile, name, dock, config.getNodeConfFile(), config.getExperimentConfFile()] + args
					log.info('running: %s', ' '.join(cmdList))
					agent = Popen(cmdList, close_fds=True, stderr=stderr)
	
				else:
					log.critical("unknown launch style '%s'", interface['execute'])
					return False
				
				self.pAgentPids[name] = agent.pid
				#Process agent once up, sends the AgentLoadDone message itself
				#self.messaging.trigger(event='AgentLoadDone', agent=name, nodes=[self.hostname] )
				
		except Exception, e:
				log.error("Agent %s on %s threw an exception %s during agent load.", name, self.hostname, e, exc_info=1)
				log.error("Sending back a RunTimeException event. This may cause the receiver to exit.")
				exc_type, exc_value, exc_tb = sys.exc_info()
				filename, line_num, func_name, text = traceback.extract_tb(exc_tb)[-1]
				filename = basename(filename)
				self.messaging.trigger(event='RuntimeException', type=exc_type.__name__, error=str(e), nodes=[self.hostname], 
									agent=self.name, func_name=func_name, filename=filename, line_num=line_num)
				return False
		
		return True

		
	def extractTarPath(self, cachepath, path):
		"""
			Internal function to extract a tar file
		"""
		if os.path.isdir(path):
			# Copy all files to cache
			# TODO: make our own recursive copy that overwrites
			if os.path.exists(cachepath):
				log.debug('Found existing dir, removing it.')
				shutil.rmtree(cachepath)

			if not os.path.exists(cachepath):
				log.debug("Copytree %s into %s", path, cachepath)
				shutil.copytree(path, cachepath) # fails if dir already exists
		else:
			# Assume file and extract appropriately
			log.debug("Extract %s into %s", path, cachepath)
			tar = tarfile.open(name=path, mode="r") 
			for m in tar.getmembers():
				tar.extract(m, cachepath)
			tar.close()


	def extractTarBuffer(self, cachepath, tardata):
		"""
			Internal function to extract files to disk from a tar data buffer
		"""
		# cache data to file
		log.debug("Extracting source to %s", cachepath)
		if os.path.exists(cachepath):
			log.warning("%s already exists, overwriting", cachepath)
		else:
			os.mkdir(cachepath)

		# decode tardata into a temp file
		scratch = tempfile.TemporaryFile()
		sp = cStringIO.StringIO(tardata)
		base64.decode(sp, scratch)
		sp.close()

		# now untar that into the selected modules directory
		scratch.seek(0)
		tar = tarfile.open(fileobj=scratch, mode="r:") # don't allow tests for compression, broken on p24 w/ fileobj
		for m in tar.getmembers():
			tar.extract(m, cachepath)
		tar.close()

