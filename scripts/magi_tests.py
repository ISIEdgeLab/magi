#!/usr/bin/env python

import signal
import logging
import os
import glob
import imp
import subprocess
import unittest2
import magi.tests
import magi.modules
import optparse

class MyTestResult(unittest2.TextTestResult):
	def getDescription(self, test):
		return test.shortDescription() or test.__class__.__name__


parser = optparse.OptionParser()
parser.add_option("-l", "--level", dest="loglevel", default=logging.WARNING, help="set the python logging level, defaults to 30 (WARNING)")
parser.add_option("", "--nobase", action="store_false", dest="dobase", default=True, help="don't run the base library tests")
parser.add_option("", "--nomodule", action="store_false", dest="domodules", default=True, help="don't run the tests in the modules dir")
parser.add_option("-f", "--filter", dest="filter", default="*_*.py", help="Filter to apply to the testsuite files")
parser.add_option("-o", "--logfile", dest="logfile", help="If given, log to the file instead of the console (stdout).")
(options, args) = parser.parse_args()

log_format = '%(asctime)s %(name)-12s %(levelname)-8s %(threadName)s %(message)s'
log_datefmt = '%m-%d %H:%M:%S'

if options.logfile:
	hdlr = logging.FileHandler(options.logfile)
else:
	hdlr = logging.StreamHandler()
	
hdlr.setFormatter(logging.Formatter(log_format, log_datefmt))
root = logging.getLogger()
root.handlers = []
root.addHandler(hdlr)
root.setLevel(options.loglevel)

loader = unittest2.TestLoader()
suite = unittest2.TestSuite()

if options.dobase:
	for f in sorted(glob.glob(os.path.join(magi.tests.__path__[0], options.filter))):
		try:
			map(suite.addTest, loader.loadTestsFromModule(imp.load_source(os.path.basename(f[:-3]), f)))
		except Exception:
			logging.error("test/load exception", exc_info=1)

if options.domodules:
	map(suite.addTest, loader.discover(magi.modules.__path__[0])) #, pattern='test*.py'))

signal.signal(signal.SIGINT, signal.SIG_DFL)
unittest2.TextTestRunner(verbosity=5, resultclass=MyTestResult).run(suite)

