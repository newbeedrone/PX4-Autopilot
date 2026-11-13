"""Exercise the real slave state machine using an open-drain two-wire bus model."""
from pathlib import Path
import os,shutil,subprocess,tempfile,unittest
ROOT=Path(__file__).resolve().parents[3]
@unittest.skipUnless(os.name=='posix' and shutil.which('g++'),'Host C++ compiler required')
class I2CSlaveTests(unittest.TestCase):
 def test_repeated_start_crc_replay_and_open_drain_ack(self):
  code=r'''
#include "i2c_simulator.hpp"
#include <assert.h>
#include <initializer_list>
#include <string.h>
using namespace protocol_test;
struct Bus {
 GpioI2CSlave slave; bool scl=true,master=true,release=true; uint64_t now=0;
 void line(bool clock,bool data) {
  scl=clock;master=data;now+=50;
  const bool next=slave.edge(scl,master&&release,now);
  if(next!=release) {release=next;assert(slave.edge(scl,master&&release,now)==release);}
 }
 void start() {line(false,true);line(true,true);line(true,false);}
 void stop() {line(false,false);line(true,false);line(true,true);assert(release);}
 bool write(uint8_t v) {
  for(unsigned b=0;b<8;b++){line(false,v&(0x80>>b));line(true,master);}
  line(false,true);line(true,true);const bool ack=!release;line(false,true);return ack;
 }
 uint8_t read(bool ack) {
  uint8_t v=0;for(unsigned b=0;b<8;b++){line(true,true);v=(v<<1)|release;line(false,true);}
  line(false,!ack);line(true,!ack);line(false,!ack);line(false,true);return v;
 }
};
int main(){
 for(Mode mode:{Mode::Gps1,Mode::SharedI2c3,Mode::Power1,Mode::Power2}) {
  Bus b;b.slave.start(mode,0x923b0817);uint8_t request[PACKET_SIZE],response[PACKET_SIZE];
  b.start();assert(!b.write((I2C_ADDRESS+1)<<1));b.stop();
  for(uint32_t sequence=1;sequence<=20;sequence++){
   packet(request,mode,0x923b0817,sequence,false);
   b.start();assert(b.write(I2C_ADDRESS<<1));for(uint8_t byte:request)assert(b.write(byte));
   b.start();assert(b.write((I2C_ADDRESS<<1)|1));
   for(unsigned i=0;i<PACKET_SIZE;i++)response[i]=b.read(i+1<PACKET_SIZE);
   b.stop();assert(valid_packet(response,PACKET_SIZE,mode,0x923b0817,sequence,true));
  }
  assert(b.slave.simulator().received()==20);assert(b.slave.simulator().transmitted()==20);assert(b.slave.simulator().errors()==0);
  b.start();assert(b.write(I2C_ADDRESS<<1));for(uint8_t byte:request)assert(b.write(byte));b.stop();
  assert(b.slave.simulator().errors()==1);assert(b.slave.simulator().received()==20);
 }
 assert(source_i2c(Mode::Gps1)==1 && fixture_i2c(Mode::Gps1)==0);
 assert(source_i2c(Mode::Power1)==1 && fixture_i2c(Mode::Power1)==1);
 assert(source_i2c(Mode::Power2)==2 && fixture_i2c(Mode::Power2)==2);
 assert(source_i2c(Mode::Gps2)==2 && fixture_i2c(Mode::Gps2)==4);
 assert(source_i2c(Mode::SharedI2c3)==3 && fixture_i2c(Mode::SharedI2c3)==0);
}
'''
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp);(path/'test.cpp').write_text(code)
   command=['g++','-std=c++14','-Wall','-Wextra','-Werror','-I'+str(ROOT/'src/systemcmds/gpio_test'),str(path/'test.cpp'),'-o',str(path/'test')]
   r=subprocess.run(command,capture_output=True,text=True);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
   r=subprocess.run([str(path/'test')],capture_output=True,text=True,timeout=10);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
