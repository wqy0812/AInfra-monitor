"""Old Docker clone3 compatibility: add a deny-only filter, never relax seccomp.

Docker 18's filter returns EPERM for unavailable clone3. glibc >=2.34 needs
ENOSYS to fall back to clone. A newer, equally restrictive ERRNO filter takes
precedence for this one syscall; all existing Docker filters still apply.
"""
import ctypes,errno,os,platform,sys

def apply():
 if platform.system()!='Linux' or platform.machine() not in ['x86_64','amd64']:return False
 libc=ctypes.CDLL(None,use_errno=True)
 # An invalid argument block can never create a process.
 result=libc.syscall(435,0,0)
 if result!=-1 or ctypes.get_errno()!=errno.EPERM:return False
 class Filter(ctypes.Structure):_fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
 class Program(ctypes.Structure):_fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
 instructions=(Filter*4)(Filter(0x20,0,0,0),Filter(0x15,0,1,435),Filter(0x06,0,0,0x00050000|errno.ENOSYS),Filter(0x06,0,0,0x7fff0000))
 program=Program(4,instructions)
 if libc.prctl(38,1,0,0,0)!=0 or libc.prctl(22,2,ctypes.byref(program),0,0)!=0:raise OSError(ctypes.get_errno(),'Cannot install restrictive clone3 compatibility filter')
 libc.syscall(435,0,0)
 if ctypes.get_errno()!=errno.ENOSYS:raise RuntimeError('clone3 compatibility filter did not take effect')
 return True

if __name__=='__main__':
 apply()
 if len(sys.argv)>1:os.execvp(sys.argv[1],sys.argv[1:])
