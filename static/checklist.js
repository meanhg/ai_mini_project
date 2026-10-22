/* 담당자의 수동 점검 기록. AI 관측 결과나 실제 서버 상태를 변경하지 않는다. */
(function (root) {
  'use strict';
  const {ref, computed, watch} = Vue;
  const labels = {pending: '미확인', in_progress: '확인 중', done: '완료', deferred: '보류'};
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '요청을 처리하지 못했습니다.');
    return body;
  }
  root.OpsChecklist = {
    create: (runId, threadId) => request('/checklists', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({run_id: runId, thread_id: threadId})
    }),
    component: {
      props: {active: Boolean, refreshKey: Number},
      setup(props) {
        const groups = ref([]), loading = ref(false), error = ref(''), notice = ref('');
        const filter = ref('all'), saving = ref({});
        let requestId = 0;
        const totals = computed(() => {
          const items = groups.value.flatMap(g => g.items);
          return {all: items.length, done: items.filter(i => i.status === 'done').length};
        });
        const visibleGroups = computed(() => groups.value.map(g => ({...g,
          visibleItems: g.items.filter(i => filter.value === 'all' || i.status === filter.value)
        })).filter(g => g.visibleItems.length));
        const complete = group => group.items.filter(i => i.status === 'done').length;
        const changed = item => item.draftStatus !== item.status || item.draftNote !== item.note;
        const timestamp = value => new Date(value).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', hour12: false});
        async function load() {
          const id = ++requestId;
          loading.value = true; error.value = '';
          try {
            const body = await request('/checklists');
            if (id !== requestId) return;
            const previous = new Map(groups.value.flatMap(g => g.items).map(i => [i.id, i]));
            groups.value = body.checklists.map(group => ({...group, items: group.items.map(item => {
              const old = previous.get(item.id);
              return {...item,
                draftStatus: old && changed(old) ? old.draftStatus : item.status,
                draftNote: old && changed(old) ? old.draftNote : item.note};
            })}));
          } catch (e) { if (id === requestId) error.value = e.message; }
          finally { if (id === requestId) loading.value = false; }
        }
        const taskText = item => item.text.replace(/get_monitoring_status/g, '모니터링 조회 도구');
        async function save(group, item, statusOnly = false) {
          if (saving.value[item.id] || !changed(item)) return;
          saving.value[item.id] = true; error.value = ''; notice.value = '';
          try {
            const body = await request(`/checklists/${encodeURIComponent(group.id)}/items/${encodeURIComponent(item.id)}`, {
              method: 'PATCH', headers: {'content-type': 'application/json'},
              body: JSON.stringify({status: item.draftStatus, note: statusOnly ? item.note : item.draftNote})
            });
            const draftNote = item.draftNote;
            Object.assign(item, body, {draftStatus: body.status, draftNote: statusOnly ? draftNote : body.note});
            notice.value = statusOnly ? '상태를 저장했습니다.' : '메모를 저장했습니다.';
          } catch (e) {
            if (statusOnly) item.draftStatus = item.status;
            error.value = e.message;
          }
          finally { saving.value[item.id] = false; }
        }
        watch(() => [props.active, props.refreshKey], () => { if (props.active) load(); }, {immediate: true});
        return {groups, visibleGroups, loading, error, notice, filter, saving, totals, labels, complete, changed, timestamp, taskText, load, save};
      },
      template: `
        <section class="checklist-panel" aria-label="대응 체크리스트">
          <header class="checklist-heading"><div><h1>대응 체크리스트</h1><p>진단에서 안내한 점검과 조치를 담당자가 직접 관리합니다.</p></div><button type="button" class="checklist-secondary" @click="load" :disabled="loading">새로고침</button></header>
          <p class="checklist-disclaimer">담당자의 수동 기록입니다. ‘완료’ 표시는 실제 서버 조치 실행이나 AI 검증 완료를 뜻하지 않습니다.</p>
          <div class="checklist-toolbar"><span>전체 {{ totals.all }}개 · 완료 {{ totals.done }}개</span><label>상태 <select v-model="filter" aria-label="체크리스트 상태 필터"><option value="all">전체</option><option v-for="(label, value) in labels" :value="value">{{ label }}</option></select></label></div>
          <p v-if="error" class="checklist-error" role="alert">{{ error }}</p>
          <p v-if="notice" class="checklist-notice" role="status">{{ notice }}</p>
          <p v-if="loading && !groups.length" class="checklist-empty">저장된 항목을 불러오는 중입니다.</p>
          <div v-else-if="!groups.length" class="checklist-empty"><h2>아직 저장한 체크리스트가 없어요</h2><p>진단 채팅에서 점검·조치가 포함된 답변의 ‘체크리스트에 추가’를 눌러주세요.</p><p>정상 상태 안내나 검증 실패 답변에는 추가 버튼이 표시되지 않습니다.</p></div>
          <p v-else-if="!visibleGroups.length" class="checklist-empty">선택한 상태의 항목이 없습니다.</p>
          <article v-for="group in visibleGroups" :key="group.id" class="checklist-group">
            <header><h2>{{ group.query }}</h2><p>원본 진단 {{ timestamp(group.diagnosis_created) }} · 완료 {{ complete(group) }}/{{ group.items.length }}</p><progress :value="complete(group)" :max="group.items.length" aria-label="담당자 점검 완료 현황"></progress></header>
            <form v-for="item in group.visibleItems" :key="item.id" class="checklist-item" @submit.prevent="save(group, item)">
              <div class="checklist-item-heading"><span class="checklist-target">{{ item.system_name }}</span><select class="checklist-status" v-model="item.draftStatus" :aria-label="item.system_name + ' 점검 상태'" :disabled="saving[item.id]" @change="save(group, item, true)"><option v-for="(label, value) in labels" :value="value">{{ label }}</option></select><span v-if="changed(item)" class="checklist-unsaved">저장하지 않은 변경</span></div>
              <p class="checklist-task">{{ taskText(item) }}</p>
              <div class="checklist-edit"><label class="checklist-note">점검 메모<textarea v-model="item.draftNote" :aria-label="item.system_name + ' 점검 메모'" maxlength="2000" rows="2" placeholder="직접 확인한 내용과 보류 사유를 기록하세요." :disabled="saving[item.id]"></textarea></label><button type="submit" class="checklist-save" :disabled="saving[item.id] || !changed(item)">{{ saving[item.id] ? '저장 중' : '메모 저장' }}</button></div>
              <p class="checklist-updated">마지막 저장 {{ timestamp(item.updated) }}</p>
            </form>
          </article>
        </section>`
    }
  };
})(window);
