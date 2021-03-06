
class ListBuffer(object):
	""" Make a buffer out of a list of buffers to avoid any appending during collection, in memory only """
	def __init__(self, buf = None):
		self.data = list()
		self.length = 0
		if buf is not None:
			self.add(buf)

	def add(self, buf):
		self.data.append(buf)
		self.length += len(buf)

	def chunk(self, suggestsize):
		return self.data[0]

	def release(self, size):
		while size > 0 and len(self.data) > 0:
			p = self.data.pop(0)
			if len(p) <= size:
				size -= len(p)
				continue
			# else size if only part of p
			self.data.insert(0, p[size:])
			break
		self.length -= size
			
	def __len__(self):
		return self.length


class FileBuffer(object):
	""" Implements a characters indexed object like a string that is stored in file(s) """

	def __init__(self, initlist=None):
		self.data = []
		if initlist is not None:
			# XXX should this accept an arbitrary sequence?
			if type(initlist) == type(self.data):
				self.data[:] = initlist
			elif isinstance(initlist, UserList):
				self.data[:] = initlist.data[:]
			else:
				self.data = list(initlist)

	# repr, lt, le, eq, ne, gt, ge, cast, cmp, mul, imul
	def __contains__(self, item): return item in self.data
	def __len__(self): return len(self.data)
	def __getitem__(self, i): return self.data[i]
	def __setitem__(self, i, item): self.data[i] = item
	def __delitem__(self, i): del self.data[i]
	def __getslice__(self, i, j):
		i = max(i, 0); j = max(j, 0)
		return self.__class__(self.data[i:j])
	def __setslice__(self, i, j, other):
		i = max(i, 0); j = max(j, 0)
		if isinstance(other, UserList):
			self.data[i:j] = other.data
		elif isinstance(other, type(self.data)):
			self.data[i:j] = other
		else:
			self.data[i:j] = list(other)
	def __delslice__(self, i, j):
		i = max(i, 0); j = max(j, 0)
		del self.data[i:j]
	def __add__(self, other):
		if isinstance(other, UserList):
			return self.__class__(self.data + other.data)
		elif isinstance(other, type(self.data)):
			return self.__class__(self.data + other)
		else:
			return self.__class__(self.data + list(other))
	def __radd__(self, other):
		if isinstance(other, UserList):
			return self.__class__(other.data + self.data)
		elif isinstance(other, type(self.data)):
			return self.__class__(other + self.data)
		else:
			return self.__class__(list(other) + self.data)
	def __iadd__(self, other):
		if isinstance(other, UserList):
			self.data += other.data
		elif isinstance(other, type(self.data)):
			self.data += other
		else:
			self.data += list(other)
		return self

	def append(self, item): self.data.append(item)
	def insert(self, i, item): self.data.insert(i, item)
	def pop(self, i=-1): return self.data.pop(i)
	def remove(self, item): self.data.remove(item)
	def count(self, item): return self.data.count(item)
	def index(self, item, *args): return self.data.index(item, *args)
	def reverse(self): self.data.reverse()
	def sort(self, *args, **kwds): self.data.sort(*args, **kwds)
	def extend(self, other):
		if isinstance(other, UserList):
			self.data.extend(other.data)
		else:
			self.data.extend(other)


