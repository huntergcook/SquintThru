// Streams Steam Frame eye openness ("<left> <right>" per line, ~60 Hz) from the
// private eye-server shm (ABI v5). Same lock-and-request protocol as frameeyeosc.
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <time.h>
#include <pthread.h>
#include <errno.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <linux/futex.h>
#define REC 0x157
int main(void){
  int fd=open("/dev/shm/eye-server.mmap",O_RDWR); struct stat st;
  if(fd<0||fstat(fd,&st)){perror("eye-server.mmap");return 1;}
  uint8_t*m=mmap(0,st.st_size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
  if(m==MAP_FAILED||*(uint32_t*)m!=5){fputs("unexpected eye-server layout\n",stderr);return 1;}
  pthread_mutex_t*mx=(pthread_mutex_t*)(m+8); volatile uint32_t*seq=(uint32_t*)(m+0x38),*req=(uint32_t*)(m+0x3c);
  struct timespec to={1,0};
  for(;;){
    int r=pthread_mutex_lock(mx); if(r==EOWNERDEAD)pthread_mutex_consistent(mx);
    uint32_t s=*seq; *req=1; pthread_mutex_unlock(mx);
    syscall(SYS_futex,seq,FUTEX_WAIT,s,&to,0,0);
    r=pthread_mutex_lock(mx); if(r==EOWNERDEAD)pthread_mutex_consistent(mx);
    float op[2]; memcpy(op,m+REC+0x79,8);
    uint32_t s2=*seq; pthread_mutex_unlock(mx);
    if(s2!=s && printf("%.3f %.3f\n",op[0],op[1])<0) return 0;
    fflush(stdout);
  }
}
