import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import { Workbook } from '@oai/artifact-tool';

const source = '/Users/miles/dev/dev-py/APSGOV7/diagnostics/urgent_order_search/restored_delivery_400000_9999s_01';
const output = '/Users/miles/Desktop/GQGA4_最终排程_40万次_9999秒.csv';
const read = async name => JSON.parse(await fs.readFile(`${source}/${name}`, 'utf8'));
const measurement = await read('measurement.json');
const report = await read('delivery_report.json');
const request = (await read('prepared_request.json')).request;
assert(measurement.result.release && measurement.result.core_audit.passed && measurement.result.audit_report.passed);
const chains = measurement.result.release.plan.chains;
const times = new Map(report.delivery_nodes.map(n => [n.node_id, n]));
const orders = new Map(report.delivery_orders.map(o => [o.source_order_id, o]));
const sourceOrders = new Map(request.orders.map(o => [o.source_order_id, o]));
const periods = new Map(request.periods.map((p, i) => [p.period_id, i + 1]));
const roles = {normal_real:'真实非过渡材',actual_transition:'真实过渡材',virtual_sphc:'虚拟过渡材'};
const local = value => value ? value.replace('T', ' ').replace('+08:00', '') : '';
const header = ['生产顺序','大辊期顺序','大辊期编号','小辊期顺序','小辊期编号','链内顺序','节点编号','原订单编号','材料类型','是否拆单片段','本片段重量_吨','小辊期总重量_吨','原订单总重量_吨_拆片重复勿汇总','宽度_mm','厚度_mm','成品牌号','热轧牌号','软硬钢类别','钢种类别','表面等级','温区下限_摄氏度','温区上限_摄氏度','客户名称','客户等级','执行标准','来源大辊期','是否提前借用','交货日期','交期截止_北京时间','本节点开始_北京时间','本节点完成_北京时间','原订单最后完成_北京时间','原订单交期状态','原订单晚交小时_重复勿汇总','是否本月新增晚交','原订单起排后等待或延期小时_重复勿汇总','来源资源编号','拆单谱系_JSON','虚拟材谱系_JSON'];
const rows = [];
for (const [ci, chain] of chains.entries()) {
  const weight = Math.round(chain.nodes.reduce((s,n)=>s+n.weight,0)*100)/100;
  for (const [ni, node] of chain.nodes.entries()) {
    const t=times.get(node.node_id), o=orders.get(node.source_order_id), attrs=node.rule_attributes;
    assert(t);
    const virtual=node.material_role==='virtual_sphc';
    assert(virtual || (o && sourceOrders.has(node.source_order_id)));
    assert(roles[node.material_role]);
    let dueEnd='';
    if(o) dueEnd=new Date(Date.parse(`${o.due_date}T00:00:00Z`)+86400000).toISOString().slice(0,10)+' 00:00:00';
    rows.push([rows.length+1,periods.get(chain.assigned_period),chain.assigned_period,ci+1,chain.chain_id,ni+1,node.node_id,node.source_order_id??'',roles[node.material_role],node.split_lineage?'是':'否',node.weight,weight,o?.original_weight??'',node.width,node.thickness,node.grade,attrs.hot_roll_grade??'',attrs.soft_hard_class??'',attrs.grade_class??'',attrs.surface_grade??'',node.min_temperature??'',node.max_temperature??'',attrs.customer_name??'',attrs.customer_grade??'',attrs.execution_standard??'',node.source_period??'',virtual?'不适用':periods.get(chain.assigned_period)<periods.get(node.source_period)?'是':'否',o?.due_date??'',dueEnd,local(t.start_at),local(t.completion_at),local(o?.completion_at),virtual?'虚拟材不计交期':o.was_backlog_at_start?'上月已欠交':o.newly_late?'本月新增晚交':'准交',o?Number(o.actual_tardiness_hours.toFixed(6)):'',virtual?'不适用':o.newly_late?'是':'否',o?Number(o.wait_tardiness_hours.toFixed(6)):'',node.source_resource_id??'',node.split_lineage?JSON.stringify(node.split_lineage):'',node.virtual_lineage?JSON.stringify(node.virtual_lineage):'']);
  }
}
assert.equal(rows.length,567);
assert.deepEqual(rows.map(r=>r[6]), report.delivery_nodes.map(n=>n.node_id));
assert.equal(new Set(rows.filter(r=>r[8]!=='虚拟过渡材').map(r=>r[7])).size,531);
const wb=Workbook.create();
const sheet=wb.worksheets.add('最终排程');
const range=sheet.getRangeByIndexes(0,0,rows.length+1,header.length);
range.values=[header,...rows];
wb.recalculate();
const values=range.values;
assert.deepEqual(values,[header,...rows]);
console.log((await wb.inspect({kind:'table',range:'最终排程!A1:K4',include:'values',tableMaxRows:4,tableMaxCols:11,maxChars:1800})).ndjson);
// CSV has no layout or cell types; retain literal identifiers and explicit local timestamps.
const escape = value => {
  const s=String(value??'');
  assert(!/^[=+@\t\r]/.test(s), 'Unexpected formula-like text');
  return /[",\r\n]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;
};
await fs.writeFile(output,'\ufeff'+values.map(row=>row.map(escape).join(',')).join('\r\n')+'\r\n',{encoding:'utf8',flag:'wx'});
console.log(JSON.stringify({output,rows:rows.length,columns:header.length,real_fragments:rows.filter(r=>r[8]!=='虚拟过渡材').length,virtual_nodes:rows.filter(r=>r[8]==='虚拟过渡材').length}));
