const BASE = import.meta.env.VITE_API_URL ?? '';

const API_UNAVAILABLE_MESSAGE = '无法连接 API 服务。请启动后端，然后刷新页面重试。';
let csrfToken = '';
let unauthorizedHandler:(()=>void)|undefined;

export class ApiError extends Error {
  constructor(
    message:string,
    readonly status:number,
    readonly code:string,
    readonly retryable:boolean,
    readonly operationId?:string,
  ){
    super(message);
    this.name = 'ApiError';
  }
}

async function request(path:string, init?:RequestInit):Promise<Response>{
  try {
    return await fetch(`${BASE}${path}`, {
      credentials:'include',
      ...init,
    });
  } catch (reason) {
    if (reason instanceof TypeError) {
      throw new ApiError(API_UNAVAILABLE_MESSAGE, 0, 'API_UNREACHABLE', true);
    }
    throw reason;
  }
}

function parsePayload(text:string):Record<string,unknown>|undefined {
  if (!text) return undefined;
  try {
    return JSON.parse(text) as Record<string,unknown>;
  } catch {
    return undefined;
  }
}

async function call<T>(path:string, init?:RequestInit):Promise<T>{
  const headers = new Headers(init?.headers);
  if(init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type','application/json');
  }
  const method = (init?.method || 'GET').toUpperCase();
  if(csrfToken && !['GET','HEAD','OPTIONS'].includes(method)) {
    headers.set('X-CSRF-Token',csrfToken);
  }
  const response = await request(path, {
    ...init,
    headers,
  });
  const text = await response.text();
  const payload = parsePayload(text);
  if(!response.ok) {
    const apiUnavailable = !payload && response.status >= 500;
    if(response.status === 401) unauthorizedHandler?.();
    throw new ApiError(
      apiUnavailable
        ? API_UNAVAILABLE_MESSAGE
        : String(payload?.message || payload?.error || '请求失败'),
      response.status,
      apiUnavailable ? 'API_UNREACHABLE' : String(payload?.code || 'UNKNOWN_ERROR'),
      Boolean(payload?.retryable),
      payload?.operationId ? String(payload.operationId) : undefined,
    );
  }
  return payload as T;
}

async function streamQa(
  path:string,
  body:object,
  onDelta:(delta:string)=>void,
):Promise<{sessionId:string;threadId:string;relation:string}>{
  const response = await request(path, {
    method:'POST',
    headers:{
      'Content-Type':'application/json',
      ...(csrfToken ? {'X-CSRF-Token':csrfToken} : {}),
    },
    credentials:'include',
    body:JSON.stringify(body),
  });
  if(!response.ok){
    if(response.status === 401) unauthorizedHandler?.();
    const text = await response.text();
    const payload = parsePayload(text);
    throw new ApiError(
      !payload && response.status >= 500
        ? API_UNAVAILABLE_MESSAGE
        : String(payload?.message || payload?.error || '答疑发送失败'),
      response.status,
      !payload && response.status >= 500
        ? 'API_UNREACHABLE'
        : String(payload?.code || 'UNKNOWN_ERROR'),
      Boolean(payload?.retryable),
      payload?.operationId ? String(payload.operationId) : undefined,
    );
  }
  if(!response.body) throw new Error('浏览器不支持流式答疑');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed:{sessionId:string;threadId:string;relation:string}|undefined;
  while(true){
    const {value,done} = await reader.read();
    buffer += decoder.decode(value,{stream:!done});
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for(const line of lines){
      if(!line.trim()) continue;
      const event = JSON.parse(line);
      if(event.type === 'delta') onDelta(event.delta);
      if(event.type === 'done') completed = event;
      if(event.type === 'error') throw new Error(event.error || '答疑生成失败');
    }
    if(done) break;
  }
  if(!completed) throw new Error('答疑流意外结束');
  return completed;
}

export const api = {
  setUnauthorizedHandler:(handler:()=>void)=>{ unauthorizedHandler = handler; },
  authMe:async()=>{
    const state = await call<import('../model/types').AuthState>('/api/auth/me');
    csrfToken = state.csrfToken;
    return state;
  },
  login:(returnTo='/')=>{
    window.location.assign(`${BASE}/api/auth/login?return_to=${encodeURIComponent(returnTo)}`);
  },
  logout:async()=>{
    await call<void>('/api/auth/logout',{method:'POST'});
    csrfToken = '';
  },
  bootstrap:()=>call<import('../model/types').Bootstrap>('/api/bootstrap'),
  updateResume:(sectionId:string,blockId='')=>call<import('../model/types').ResumePosition>(`/api/sections/${sectionId}/resume`,{method:'PUT',body:JSON.stringify({blockId})}),
  aiRuntime:()=>call<import('../model/types').AiRuntime>('/api/runtime/ai'),
  updateAiRuntime:(body:object)=>call<import('../model/types').AiRuntime>('/api/runtime/ai',{method:'PUT',body:JSON.stringify(body)}),
  createPlan:(body:object,idempotencyKey:string)=>call<import('../model/types').Series>('/api/plans',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':idempotencyKey},body:JSON.stringify(body)}),
  series:(id:string)=>call<import('../model/types').Series>(`/api/series/${id}`),
  deleteSeries:(id:string)=>call<void>(`/api/series/${id}`,{method:'DELETE'}),
  deleteBook:(id:string)=>call<void>(`/api/books/${id}`,{method:'DELETE'}),
  chapter:(id:string)=>call<import('../model/types').Chapter>(`/api/chapters/${id}/generate`,{method:'POST'}),
  section:(id:string)=>call<import('../model/types').Section>(`/api/sections/${id}`),
  generateSection:(id:string)=>call<import('../model/types').Section>(`/api/sections/${id}/generate`,{method:'POST'}),
  quiz:(id:string,quizSetId:string,answers:number[][],idempotencyKey:string)=>call<import('../model/types').QuizResult>(`/api/sections/${id}/quiz`,{
    method:'POST',
    headers:{'Content-Type':'application/json','Idempotency-Key':idempotencyKey},
    body:JSON.stringify({quizSetId,answers}),
  }),
  learningTask:(id:string)=>call<import('../model/types').LearningTask>(`/api/learning-tasks/${id}`),
  retryLearningTask:(id:string)=>call<import('../model/types').LearningTask>(`/api/learning-tasks/${id}/retry`,{method:'POST'}),
  ask:(id:string,blockId:string,question:string,threadId?:string,forceRelation?:'follow_up'|'new_question')=>call<import('../model/types').QaAnswer>(`/api/sections/${id}/ask`,{method:'POST',body:JSON.stringify({blockId,question,threadId,forceRelation})}),
  askStream:(id:string,blockId:string,question:string,onDelta:(delta:string)=>void,threadId?:string,forceRelation?:'follow_up'|'new_question')=>streamQa(`/api/sections/${id}/ask/stream`,{blockId,question,threadId,forceRelation},onDelta),
  correctQa:(id:string,threadId:string,targetThreadId:string)=>call<import('../model/types').QaCorrection>(`/api/sections/${id}/qa/threads/${threadId}`,{method:'PATCH',body:JSON.stringify({relation:'follow_up',targetThreadId})}),
  askMe:(id:string,answer='')=>call<import('../model/types').AskMe>(`/api/sections/${id}/ask-me`,{method:'POST',body:JSON.stringify({answer})}),
  note:(id:string,content:object)=>call<import('../model/types').Note>(`/api/sections/${id}/note`,{method:'PATCH',body:JSON.stringify({content})}),
  uploadPractice:(id:string,file:File)=>call<import('../model/types').Attachment>(`/api/chapters/${id}/practice/attachments`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file}),
  practice:(id:string,content:object,attachmentIds:string[])=>call<import('../model/types').Practice>(`/api/chapters/${id}/practice`,{method:'POST',body:JSON.stringify({content,attachmentIds})}),
  uploadCapstone:(id:string,file:File)=>call<import('../model/types').Attachment>(`/api/books/${id}/capstone/attachments`,{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file}),
  capstone:(id:string,content:object,attachmentIds:string[])=>call<import('../model/types').Capstone>(`/api/books/${id}/capstone`,{method:'POST',body:JSON.stringify({content,attachmentIds})}),
  updateChapter:(id:string,body:object)=>call<import('../model/types').Chapter>(`/api/chapters/${id}`,{method:'PATCH',body:JSON.stringify(body)}),
  addChapter:(id:string,body:object)=>call<import('../model/types').Chapter>(`/api/books/${id}/chapters`,{method:'POST',body:JSON.stringify(body)}),
  deleteChapter:(id:string)=>call<void>(`/api/chapters/${id}`,{method:'DELETE'}),
};
