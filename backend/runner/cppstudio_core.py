#!/usr/bin/env python3
"""Local G++ + GDB backend for C++ Studio. Standard library only."""
from __future__ import annotations
import json, os, re, shutil, subprocess, tempfile, threading, queue, time, webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("CPP_STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("CPP_STUDIO_PORT", "8765"))
MAX_BODY = 2 * 1024 * 1024
MAX_STEPS = 800
COMPILE_TIMEOUT = 25
RUN_TIMEOUT = 8
TRACE_TIMEOUT = 25


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def mi_quote(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=True)


class MIParser:
    def __init__(self, text):
        self.s = text
        self.i = 0

    def _skip(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def _string(self):
        assert self.s[self.i] == '"'
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]; self.i += 1
            if c == '"':
                return ''.join(out)
            if c != '\\':
                out.append(c); continue
            if self.i >= len(self.s): break
            e = self.s[self.i]; self.i += 1
            table = {'n':'\n','r':'\r','t':'\t','b':'\b','f':'\f','v':'\v','a':'\a','\\':'\\','"':'"'}
            if e in table: out.append(table[e])
            elif e == 'x':
                m = re.match(r'[0-9a-fA-F]{1,2}', self.s[self.i:])
                if m:
                    out.append(chr(int(m.group(),16))); self.i += len(m.group())
            elif e in '01234567':
                digits = e
                while self.i < len(self.s) and len(digits) < 3 and self.s[self.i] in '01234567':
                    digits += self.s[self.i]; self.i += 1
                out.append(chr(int(digits,8)))
            else: out.append(e)
        return ''.join(out)

    def _ident(self):
        start = self.i
        while self.i < len(self.s) and (self.s[self.i].isalnum() or self.s[self.i] in '_-'):
            self.i += 1
        return self.s[start:self.i]

    def _value(self):
        self._skip()
        if self.i >= len(self.s): return ''
        c = self.s[self.i]
        if c == '"': return self._string()
        if c == '{':
            self.i += 1
            value = self._results('}')
            if self.i < len(self.s) and self.s[self.i] == '}': self.i += 1
            return value
        if c == '[':
            self.i += 1
            values = []
            while self.i < len(self.s):
                self._skip()
                if self.i < len(self.s) and self.s[self.i] == ']':
                    self.i += 1; break
                start = self.i
                ident = self._ident()
                self._skip()
                if ident and self.i < len(self.s) and self.s[self.i] == '=':
                    self.i += 1
                    item = {ident: self._value()}
                else:
                    self.i = start
                    item = self._value()
                values.append(item)
                self._skip()
                if self.i < len(self.s) and self.s[self.i] == ',': self.i += 1
                elif self.i < len(self.s) and self.s[self.i] == ']':
                    self.i += 1; break
                else: break
            return values
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in ',}]': self.i += 1
        return self.s[start:self.i].strip()

    def _results(self, end=None):
        out = {}
        while self.i < len(self.s):
            self._skip()
            if end and self.i < len(self.s) and self.s[self.i] == end: break
            key = self._ident()
            if not key: break
            self._skip()
            if self.i >= len(self.s) or self.s[self.i] != '=': break
            self.i += 1
            val = self._value()
            if key in out:
                if not isinstance(out[key], list): out[key] = [out[key]]
                out[key].append(val)
            else: out[key] = val
            self._skip()
            if self.i < len(self.s) and self.s[self.i] == ',': self.i += 1
            elif end and self.i < len(self.s) and self.s[self.i] == end: break
            else: break
        return out

    def parse(self):
        return self._results()


def parse_mi_record(line):
    m = re.match(r'^(\d*)([\^*=+~@&])(.*)$', line)
    if not m: return None
    token, kind, rest = m.groups()
    if kind in '~@&':
        return {'token': token, 'kind': kind, 'stream': MIParser(rest)._value()}
    if ',' in rest:
        cls, payload = rest.split(',', 1)
        data = MIParser(payload).parse()
    else:
        cls, data = rest, {}
    return {'token': token, 'kind': kind, 'class': cls, **data}


class MIClient:
    def __init__(self, executable):
        self.proc = subprocess.Popen(
            [executable, '--quiet', '--nx', '--interpreter=mi2'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1
        )
        self.q = queue.Queue()
        self.pending_stops = []
        self.target_output = ''
        self.console_output = ''
        self.log_output = ''
        self.token = 0
        self.closed = False
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line.rstrip('\r\n'))
        finally:
            self.q.put(None)

    def _line(self, timeout):
        try: return self.q.get(timeout=timeout)
        except queue.Empty: raise TimeoutError('GDB không phản hồi đúng thời hạn.')

    def _handle(self, line):
        if line is None: raise RuntimeError('GDB đã đóng tiến trình.')
        rec = parse_mi_record(line)
        if not rec: return
        if rec['kind'] == '@': self.target_output += rec.get('stream','')
        elif rec['kind'] == '~': self.console_output += rec.get('stream','')
        elif rec['kind'] == '&': self.log_output += rec.get('stream','')
        elif rec['kind'] == '*' and rec.get('class') == 'stopped': self.pending_stops.append(rec)

    def command(self, command, timeout=4):
        if self.closed: raise RuntimeError('Phiên GDB đã đóng.')
        self.token += 1
        token = str(self.token)
        self.proc.stdin.write(token + command + '\n'); self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0: raise TimeoutError('GDB hết thời gian khi chạy lệnh: ' + command[:70])
            line = self._line(left)
            if line is None: raise RuntimeError('GDB đã kết thúc bất ngờ.')
            if line.startswith(token + '^'):
                rec = parse_mi_record(line)
                if rec and rec.get('class') == 'error':
                    raise RuntimeError(rec.get('msg','GDB báo lỗi: '+command))
                return rec or {}
            self._handle(line)

    def wait_stop(self, timeout=RUN_TIMEOUT):
        if self.pending_stops: return self.pending_stops.pop(0)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0: raise TimeoutError('Chương trình chạy quá lâu hoặc đang chờ dữ liệu đầu vào.')
            line = self._line(left)
            if line is None: raise RuntimeError('GDB đóng trước khi chương trình dừng.')
            rec = parse_mi_record(line)
            if rec and rec.get('kind') == '*' and rec.get('class') == 'stopped': return rec
            self._handle(line)

    def drain_streams(self):
        while True:
            try: line = self.q.get_nowait()
            except queue.Empty: break
            if line is None: continue
            self._handle(line)
        out = self.target_output
        self.target_output = ''
        return out

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write('9999-gdb-exit\n'); self.proc.stdin.flush()
                self.proc.wait(timeout=1.2)
        except Exception:
            try: self.proc.terminate(); self.proc.wait(timeout=1)
            except Exception:
                try: self.proc.kill()
                except Exception: pass


def mi_list(value, key=None):
    if value is None: return []
    if not isinstance(value, list): value = [value]
    out = []
    for item in value:
        if key and isinstance(item, dict) and key in item: out.append(item[key])
        else: out.append(item)
    return out


def parse_gdb_scalar(value, typ=''):
    if value is None: return None
    raw = str(value).strip()
    if raw in ('true','false'): return raw == 'true'
    if '*' in typ:
        m = re.search(r'0x[0-9a-fA-F]+', raw)
        if raw in ('nullptr','0x0','(nil)') or (m and int(m.group(0),16)==0):
            return {'t':'ptr','address':None,'status':'null','targetId':None}
        return {'t':'ptr','address':m.group(0) if m else None,'status':'unknown' if m else 'uninitialized','targetId':None}
    if typ.endswith('&'):
        # References are displayed as their current value; the real address is kept separately.
        pass
    if 'char' in typ and (raw.startswith("'") or re.search(r"'[^']'", raw)):
        m = re.search(r"'((?:\\.|[^'])*)'", raw)
        if m: return m.group(1).replace('\\n','\n').replace('\\t','\t')
    if 'string' in typ or 'basic_string' in typ:
        m = re.search(r'"((?:\\.|[^"\\])*)"', raw)
        if m: return bytes(m.group(1),'utf-8').decode('unicode_escape')
    # GDB may print a pretty-printer summary followed by = { elements }.
    aggregate = raw[raw.find('=')+1:].strip() if '=' in raw and '{' in raw[raw.find('=')+1:] else raw
    if aggregate.startswith('{') and aggregate.endswith('}'):
        inner = aggregate[1:-1].strip()
        parts=[]; buf=''; depth=0; quote=None; esc=False
        for ch in inner:
            if esc: buf += ch; esc=False; continue
            if ch == '\\': buf += ch; esc=True; continue
            if quote:
                buf += ch
                if ch == quote: quote=None
                continue
            if ch in ('"',"'"): quote=ch; buf += ch; continue
            if ch in '{[(': depth += 1
            elif ch in '}])': depth -= 1
            if ch == ',' and depth == 0: parts.append(buf.strip()); buf=''
            else: buf += ch
        if buf.strip(): parts.append(buf.strip())
        if parts and all('=' in p for p in parts) and not re.match(r'\s*[-+]?\d', parts[0]):
            vals=[]
            for part in parts:
                k,v=part.split('=',1); vals.append([k.strip(),parse_gdb_scalar(v.strip())])
            return {'t':'m','v':vals}
        return {'t':'a','v':[parse_gdb_scalar(x) for x in parts],'n':len(parts)}
    if raw.startswith('"') and raw.endswith('"'):
        try: return json.loads(raw)
        except Exception: return raw[1:-1]
    if re.fullmatch(r'[-+]?0[xX][0-9a-fA-F]+', raw):
        try: return int(raw,16)
        except Exception: pass
    if re.fullmatch(r'[-+]?\d+', raw):
        try: return int(raw)
        except Exception: pass
    if re.fullmatch(r'[-+]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][-+]?\d+)?', raw):
        try: return float(raw)
        except Exception: pass
    return raw if raw else None


def normalize_address(value):
    if value is None: return None
    m = re.search(r'0x[0-9a-fA-F]+', str(value))
    return m.group(0) if m else None


class NativeTracer:
    def __init__(self, gdb_path, exe, source_path, stdin_path, max_steps=MAX_STEPS):
        self.mi = MIClient(gdb_path)
        self.exe = exe
        self.source_path = os.path.normcase(os.path.realpath(source_path))
        self.stdin_path = stdin_path
        self.max_steps = max_steps
        self.steps = []
        self.stdout_parts = []
        self.last_frame = None
        self.globals = []
        self.last_snapshot = None

    def _same_source(self, frame):
        full = frame.get('fullname') or frame.get('file') or ''
        if not full: return False
        try:
            if os.path.normcase(os.path.realpath(full)) == self.source_path: return True
        except Exception: pass
        return os.path.basename(full).lower() == os.path.basename(self.source_path).lower()

    def _eval(self, expression, timeout=2):
        try:
            r = self.mi.command('-data-evaluate-expression ' + mi_quote(expression), timeout=timeout)
            return r.get('value')
        except Exception:
            return None

    def _address(self, expression):
        return normalize_address(self._eval('&(' + expression + ')'))

    def _var_create(self, expression):
        try:
            r = self.mi.command('-var-create - * ' + mi_quote(expression), timeout=2)
            return r
        except Exception:
            return None

    def _expand_children(self, varobj, expression, parent_id, frame_id, region, depth=0, limit=64):
        if depth >= 2 or not varobj: return []
        try:
            res = self.mi.command('-var-list-children --all-values ' + varobj, timeout=2)
        except Exception:
            return []
        raw = mi_list(res.get('children'), 'child')
        cells=[]
        for child in raw[:limit]:
            if not isinstance(child,dict): continue
            exp = str(child.get('exp') or child.get('name') or '')
            if not exp: continue
            child_expr = expression + exp if exp.startswith('[') or exp.startswith('*') else expression + '.' + exp
            name = expression + exp
            typ = str(child.get('type') or 'unknown')
            valraw = child.get('value')
            cid = parent_id + '/' + str(child.get('name') or exp)
            addr = self._address(child_expr)
            value = parse_gdb_scalar(valraw, typ)
            cell={'id':cid,'address':addr or '—','name':name,'type':typ,'value':value,'region':region,'status':'live','parentId':parent_id}
            if frame_id: cell['frame']=frame_id
            cells.append(cell)
            child_count=int(child.get('numchild') or 0)
            if child_count and '*' not in typ and 'basic_string' not in typ and 'std::string' not in typ:
                cells.extend(self._expand_children(str(child.get('name') or ''), child_expr, cid, frame_id, region, depth+1, limit))
        return cells

    def _collect_global_names(self):
        try:
            res = self.mi.command('-symbol-info-variables --max-results 300', timeout=3)
            groups = mi_list(res.get('symbols'))
            for group in groups:
                if not isinstance(group,dict): continue
                filename = group.get('filename','')
                if filename and os.path.basename(str(filename)).lower()!=os.path.basename(self.source_path).lower(): continue
                for sym in mi_list(group.get('symbols')):
                    if isinstance(sym,dict) and sym.get('name'):
                        name=str(sym['name'])
                        if name not in [x['name'] for x in self.globals]:
                            self.globals.append({'name':name,'type':str(sym.get('type') or 'unknown')})
        except Exception:
            self.globals=[]

    def _collect_snapshot(self, stop, start=False):
        frame_res = self.mi.command('-stack-list-frames', timeout=3)
        raw_frames = mi_list(frame_res.get('stack'), 'frame')
        source_frames = [f for f in raw_frames if isinstance(f,dict) and self._same_source(f)]
        if not source_frames and raw_frames and isinstance(raw_frames[0],dict): source_frames=[raw_frames[0]]
        if not source_frames: return None
        if not self.globals: self._collect_global_names()
        # GDB returns newest frame first; the visual memory model stores outer-to-inner.
        frame_data=[]; pending_ptrs=[]; current_v={}; current_types={}; current_params={}; current_func='main'; current_line=0; current_id=None
        for raw_frame in reversed(source_frames):
            level=int(raw_frame.get('level') or 0)
            self.mi.command(f'-stack-select-frame {level}', timeout=2)
            func=str(raw_frame.get('func') or 'hàm')
            fid=f'frame-{level}-{func}'
            line=int(raw_frame.get('line') or 0)
            vars_res=self.mi.command('-stack-list-variables --all-values', timeout=2)
            variables=mi_list(vars_res.get('variables'))
            frame_cells=[]; direct_values={}; type_map={}; param_names=[]
            for variable in variables[:80]:
                if not isinstance(variable,dict) or not variable.get('name'): continue
                name=str(variable['name'])
                if name.startswith('$') or name in ('__PRETTY_FUNCTION__',): continue
                varres=self._var_create(name)
                typ=str((varres or {}).get('type') or 'unknown')
                rawval=(varres or {}).get('value', variable.get('value'))
                val=parse_gdb_scalar(rawval,typ)
                addr=self._address(name)
                cid=f'{fid}:{name}'
                cell={'id':cid,'address':addr or '—','name':name,'type':typ,'value':val,'region':'stack','status':'live','frame':fid,'frameName':func}
                frame_cells.append(cell)
                direct_values[name]=val; type_map[name]=typ
                if str(variable.get('arg','0'))=='1': param_names.append(name)
                if '*' not in typ and '&' not in typ and int((varres or {}).get('numchild') or 0)>0 and 'basic_string' not in typ and 'std::string' not in typ:
                    frame_cells.extend(self._expand_children(str((varres or {}).get('name') or ''), name, cid, fid, 'stack'))
                if '*' in typ and val and isinstance(val,dict) and val.get('t')=='ptr':
                    pending_ptrs.append({'cell':cell,'expr':name,'type':typ,'address':val.get('address'),'level':level})
                try:
                    if varres and varres.get('name'): self.mi.command('-var-delete '+str(varres['name']), timeout=1)
                except Exception: pass
            frame_data.append({'id':fid,'name':func,'cells':frame_cells})
            if level==0 or current_id is None:
                current_v=direct_values; current_types=type_map; current_params=param_names
                current_func=func; current_line=line; current_id=fid
        # Optional global symbols from the current translation unit.
        global_cells=[]
        for glob in self.globals[:80]:
            name=glob['name']; expr='::'+name
            varres=self._var_create(expr)
            if not varres: continue
            typ=str(varres.get('type') or glob.get('type') or 'unknown')
            rawval=varres.get('value')
            val=parse_gdb_scalar(rawval,typ)
            addr=self._address(expr)
            cell={'id':'global:'+name,'address':addr or '—','name':name,'type':typ,'value':val,'region':'global','status':'live'}
            global_cells.append(cell)
            if '*' in typ and isinstance(val,dict) and val.get('t')=='ptr':
                pending_ptrs.append({'cell':cell,'expr':expr,'type':typ,'address':val.get('address'),'level':0})
            try: self.mi.command('-var-delete '+str(varres.get('name')), timeout=1)
            except Exception: pass
        all_cells=[]
        for fr in frame_data: all_cells.extend(fr['cells'])
        all_cells.extend(global_cells)
        address_map={c['address']:c['id'] for c in all_cells if c.get('address') and c['address']!='—'}
        heap_cells=[]; heap_by_address={}
        for item in pending_ptrs:
            cell=item['cell']; addr=normalize_address(item.get('address'))
            if not addr:
                prior=cell.get('value') or {}
                status='null' if prior.get('status')=='null' else 'uninitialized' if prior.get('status')=='uninitialized' else 'unknown'
                cell['value']={'t':'ptr','address':None,'status':status,'targetId':None}
                cell['targetStatus']=status
                continue
            target_id=address_map.get(addr)
            if target_id:
                cell['value']={'t':'ptr','address':addr,'status':'live','targetId':target_id}
                cell.update({'targetId':target_id,'targetAddress':addr,'targetStatus':'live'})
                continue
            pointee=item['type'].replace('*','').strip()
            if pointee=='void' or '(*' in item['type']:
                cell['value']={'t':'ptr','address':addr,'status':'unknown','targetId':None}; cell.update({'targetAddress':addr,'targetStatus':'unknown'}); continue
            try:
                self.mi.command(f"-stack-select-frame {item['level']}", timeout=2)
                deref=self._eval('*(' + item['expr'] + ')')
            except Exception: deref=None
            if deref is not None:
                if addr not in heap_by_address:
                    hid='heap:'+addr
                    hcell={'id':hid,'address':addr,'name':'*'+item['expr'],'type':pointee,'value':parse_gdb_scalar(deref,pointee),'region':'heap','status':'live'}
                    heap_by_address[addr]=hid; heap_cells.append(hcell)
                target_id=heap_by_address[addr]
                cell['value']={'t':'ptr','address':addr,'status':'live','targetId':target_id}
                cell.update({'targetId':target_id,'targetAddress':addr,'targetStatus':'live'})
            else:
                cell['value']={'t':'ptr','address':addr,'status':'unknown','targetId':None}
                cell.update({'targetAddress':addr,'targetStatus':'unknown'})
        try: self.mi.command('-stack-select-frame 0', timeout=1)
        except Exception: pass
        snap={'globals':global_cells,'frames':frame_data,'heap':heap_cells,'retired':[],'events':[]}
        step={'n':current_line,'f':current_func,'frame':current_id,'v':current_v,'types':current_types,'ram':snap}
        if start: step['start']=True
        if current_params: step['parameterNames']=current_params
        if self.last_frame is not None and current_id != self.last_frame:
            step['enter']=True
            step['parameterNames']=current_params or list(current_v.keys())
        self.last_frame=current_id
        return step

    def run(self):
        mi=self.mi
        try:
            for cmd in ['-gdb-set pagination off','-gdb-set confirm off','-gdb-set print pretty on','-gdb-set print elements 64','-gdb-set print max-depth 3','-gdb-set mi-async on']:
                try: mi.command(cmd, timeout=2)
                except Exception: pass
            mi.command('-file-exec-and-symbols '+mi_quote(self.exe), timeout=3)
            mi.command('-break-insert main', timeout=3)
            run_cmd='run < "'+self.stdin_path.replace('\\','/')+'"'
            mi.command('-interpreter-exec console '+mi_quote(run_cmd), timeout=5)
            stop=mi.wait_stop(timeout=RUN_TIMEOUT)
            if stop.get('reason') not in ('breakpoint-hit','end-stepping-range','function-finished'):
                return {'steps':[],'stdout':'','message':'Không dừng được tại main().','debug_output':mi.log_output}
            delta=mi.drain_streams()
            if delta: self.stdout_parts.append(delta)
            first=self._collect_snapshot(stop,start=True)
            if first:
                first['stdout_delta']=delta
                self.steps.append(first)
            final_reason=''; final_delta=''; exit_code=None; trace_started=time.monotonic()
            while len(self.steps)<self.max_steps:
                if time.monotonic()-trace_started > TRACE_TIMEOUT:
                    final_reason='Đã dừng trace sau 25 giây để tránh chiếm tài nguyên quá lâu.'; break
                try:
                    mi.command('-exec-step', timeout=2)
                    stop=mi.wait_stop(timeout=RUN_TIMEOUT)
                except TimeoutError as e:
                    final_reason=str(e); break
                except Exception as e:
                    final_reason=str(e); break
                delta=mi.drain_streams()
                if delta: self.stdout_parts.append(delta)
                reason=stop.get('reason','')
                if reason in ('exited-normally','exited','exited-signalled'):
                    final_reason=''; final_delta=delta
                    try: exit_code=int(stop.get('exit-code','0'),0)
                    except Exception: exit_code=0 if reason=='exited-normally' else None
                    break
                if reason=='signal-received':
                    final_reason='Chương trình dừng do tín hiệu '+str(stop.get('signal-name') or 'runtime error')+'.'
                    snap=self._collect_snapshot(stop)
                    if snap:
                        snap.update({'bad':final_reason,'stdout_delta':delta})
                        self.steps.append(snap)
                    break
                snap=self._collect_snapshot(stop)
                if not snap: continue
                # Step out of standard-library/system-header frames until user source resumes.
                skipped=0
                while skipped<12:
                    frame_res=mi.command('-stack-info-frame', timeout=2)
                    frame=frame_res.get('frame') or {}
                    if not frame or self._same_source(frame): break
                    mi.command('-exec-finish', timeout=2)
                    stop=mi.wait_stop(timeout=RUN_TIMEOUT)
                    delta2=mi.drain_streams()
                    if delta2: self.stdout_parts.append(delta2); delta += delta2
                    snap=self._collect_snapshot(stop)
                    if not snap: break
                    skipped+=1
                snap['stdout_delta']=delta
                if self.steps and snap.get('frame') != self.steps[-1].get('frame'):
                    snap['enter']=True
                self.steps.append(snap)
            else:
                final_reason=f'Đã đạt giới hạn {self.max_steps} bước mô phỏng.'
            # If inferior exited normally, add a final marker.
            last=self.steps[-1] if self.steps else None
            if last:
                if final_reason:
                    if not last.get('bad'):
                        self.steps.append({'n':last.get('n',0),'f':last.get('f',''),'frame':last.get('frame'),'v':last.get('v',{}),'types':last.get('types',{}),'ram':last.get('ram',{}),'bad':final_reason,'stdout_delta':final_delta})
                else:
                    self.steps.append({'n':0,'f':last.get('f',''),'frame':last.get('frame'),'v':last.get('v',{}),'types':last.get('types',{}),'ram':last.get('ram',{}),'done':True,'stdout_delta':final_delta})
            return {'steps':self.steps,'stdout':''.join(self.stdout_parts),'exit_code':exit_code,'message':final_reason or 'G++ biên dịch thành công · GDB đã thu thập các trạng thái thực thi.','debug_output':mi.log_output}
        finally:
            mi.close()


def compile_source(source, standard, directory, debug=False):
    compiler=shutil.which('g++')
    if not compiler: return None, 'Không tìm thấy g++. Hãy cài MinGW-w64/MSYS2 hoặc GCC và thêm thư mục bin vào PATH.'
    src=Path(directory)/'main.cpp'
    exe=Path(directory)/('program.exe' if os.name=='nt' else 'program')
    src.write_text(source,encoding='utf-8')
    flags=['-std='+standard,'-Wall','-Wextra','-pipe']
    if debug: flags += ['-g','-O0','-fno-inline','-fno-omit-frame-pointer']
    else: flags += ['-O0']
    cmd=[compiler,*flags,str(src),'-o',str(exe)]
    try:
        result=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=COMPILE_TIMEOUT,cwd=directory)
    except subprocess.TimeoutExpired:
        return None,'Quá thời gian biên dịch.'
    if result.returncode != 0:
        return {'compiler_output':result.stdout+result.stderr,'source':str(src),'exe':str(exe)}, 'Lỗi biên dịch G++.'
    return {'compiler_output':result.stdout+result.stderr,'source':str(src),'exe':str(exe)}, None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT),**kwargs)
    def log_message(self,fmt,*args): print('[HTTP]',fmt%args)
    def _json(self,code,data):
        payload=jdump(data).encode('utf-8')
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(payload))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(payload)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/health':
            self._json(200,{'g++':shutil.which('g++'),'gdb':shutil.which('gdb'),'ready':bool(shutil.which('g++')),'debug_ready':bool(shutil.which('g++') and shutil.which('gdb')),'platform':os.name}); return
        if path=='/': self.path='/CPP_Studio_Gpp.html'
        elif path!='/CPP_Studio_Gpp.html':
            self.send_error(404); return
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        if path not in ('/api/run','/api/trace'):
            self._json(404,{'message':'API không tồn tại.'}); return
        if self.headers.get('Origin') not in (None, f'http://{HOST}:{PORT}') or self.headers.get_content_type() != 'application/json':
            self._json(403,{'message':'Invalid request origin or content type.'}); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>MAX_BODY: raise ValueError('Dữ liệu gửi lên không hợp lệ hoặc vượt quá 2 MB.')
            data=json.loads(self.rfile.read(length).decode('utf-8'))
            source=str(data.get('source',''))
            if not source.strip(): raise ValueError('Chưa có mã C++ để biên dịch.')
            if len(source)>MAX_BODY: raise ValueError('Mã nguồn vượt quá giới hạn 2 MB.')
            standard=str(data.get('standard','c++20'))
            if standard not in ('c++11','c++14','c++17','c++20','c++23'): standard='c++20'
            stdin=str(data.get('stdin',''))
            with tempfile.TemporaryDirectory(prefix='cppstudio_') as td:
                compiled, error=compile_source(source,standard,td,debug=(path=='/api/trace'))
                if error:
                    code=400 if 'Lỗi biên dịch' in error else 503
                    self._json(code,{'ok':False,'message':error,'compiler_output':(compiled or {}).get('compiler_output','')}); return
                if path=='/api/run':
                    inp=Path(td)/'stdin.txt'; inp.write_text(stdin,encoding='utf-8')
                    try:
                        result=subprocess.run([compiled['exe']],input=stdin,text=True,capture_output=True,encoding='utf-8',errors='replace',timeout=RUN_TIMEOUT,cwd=td)
                        self._json(200,{'ok':True,'message':'G++ biên dịch và chạy xong.','compiler_output':compiled['compiler_output'],'stdout':result.stdout,'stderr':result.stderr,'exit_code':result.returncode})
                    except subprocess.TimeoutExpired as e:
                        self._json(200,{'ok':False,'message':'Chương trình chạy quá thời gian.','compiler_output':compiled['compiler_output'],'stdout':e.stdout or '','stderr':e.stderr or '','exit_code':None})
                    return
                gdb=shutil.which('gdb')
                if not gdb:
                    inp=Path(td)/'stdin.txt'; inp.write_text(stdin,encoding='utf-8')
                    try:
                        result=subprocess.run([compiled['exe']],input=stdin,text=True,capture_output=True,encoding='utf-8',errors='replace',timeout=RUN_TIMEOUT,cwd=td)
                        stdout,stderr,exit_code=result.stdout,result.stderr,result.returncode
                    except subprocess.TimeoutExpired as e:
                        stdout,stderr,exit_code=e.stdout or '',e.stderr or '',None
                    self._json(200,{'ok':True,'message':'Đã biên dịch bằng G++, nhưng chưa tìm thấy GDB nên chưa thể mô phỏng từng bước. Hãy cài GDB và thêm vào PATH.','compiler_output':compiled['compiler_output'],'stdout':stdout,'stderr':stderr,'exit_code':exit_code,'steps':[],'needs_gdb':True}); return
                inp=Path(td)/'stdin.txt'; inp.write_text(stdin,encoding='utf-8')
                try:
                    tracer=NativeTracer(gdb,compiled['exe'],compiled['source'],str(inp))
                    trace=tracer.run()
                    self._json(200,{'ok':True,'compiler_output':compiled['compiler_output'],'stderr':'','exit_code':0,'steps':trace.get('steps',[]),'stdout':trace.get('stdout',''),'exit_code':trace.get('exit_code'),'message':trace.get('message',''),'debug_output':trace.get('debug_output','')})
                except Exception as e:
                    self._json(200,{'ok':False,'message':'G++ đã biên dịch nhưng GDB không thể tạo trace: '+str(e),'compiler_output':compiled['compiler_output'],'steps':[],'stdout':''})
        except Exception as e:
            self._json(400,{'ok':False,'message':str(e)})


def main():
    if not (ROOT/'CPP_Studio_Gpp.html').exists():
        print('Không tìm thấy CPP_Studio_Gpp.html trong thư mục chạy.'); return
    health=f"g++: {'OK' if shutil.which('g++') else 'THIẾU'} | gdb: {'OK' if shutil.which('gdb') else 'THIẾU'}"
    print('C++ Studio local backend'); print(health)
    print(f'Mở http://{HOST}:{PORT} trong trình duyệt. Chỉ chạy mã nguồn bạn tin tưởng.')
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    try:
        if os.environ.get('CPP_STUDIO_NO_BROWSER') != '1':
            threading.Timer(0.8,lambda:webbrowser.open(f'http://{HOST}:{PORT}')).start()
        server.serve_forever()
    except KeyboardInterrupt: print('\nĐã dừng server.')
    finally: server.server_close()

if __name__=='__main__': main()
