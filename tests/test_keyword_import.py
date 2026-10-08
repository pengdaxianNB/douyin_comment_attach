from pathlib import Path

import pytest

from app.db import DB
from app.keyword_csv import read_keyword_csv,write_keyword_template,KEYWORD_CSV_EXAMPLES


@pytest.mark.parametrize('encoding',['utf-8','utf-8-sig','gb18030','gbk'])
def test_csv_accepts_excel_encodings_blank_rows_and_quoted_commas(tmp_path,encoding):
    path = tmp_path/'words.csv'
    path.write_text('\n 关键词 \r\n 控糖 \r\n\r\n"健康,管理"\r\n控糖\r\n',encoding=encoding)
    assert read_keyword_csv(path) == ['控糖','健康,管理','控糖']


@pytest.mark.parametrize('header',['关键词','搜索词','搜索关键词','keyword',' Keyword '])
def test_csv_accepts_common_headers(tmp_path,header):
    path = tmp_path/'words.csv'
    path.write_text(header+'\n测试词\n',encoding='utf-8')
    assert read_keyword_csv(path) == ['测试词']


@pytest.mark.parametrize('text,reason',[
    ('','没有可导入'),
    ('关键词\n\n','没有可导入'),
    ('测试词\n另一个词\n','第一行表头'),
    ('关键词\n有效词\n错误,多列\n','第 3 行有多列'),
    ('关键词\n有效词\n"未闭合','第 3 行格式错误'),
    ('关键词\n"第一行\n第二行"\n','包含换行'),
    ('关键词\n含\x00字符','无效字符'),
])
def test_csv_rejects_invalid_files_with_actionable_errors(tmp_path,text,reason):
    path = tmp_path/'words.csv'
    path.write_text(text,encoding='utf-8')
    with pytest.raises(ValueError,match=reason):
        read_keyword_csv(path)


@pytest.mark.parametrize('search',[False,True])
def test_batch_deduplicates_without_changing_existing_ids_or_other_source(tmp_path,search):
    db = DB(tmp_path/'test.sqlite3')
    table = 'search_keywords' if search else 'keywords'
    other = 'keywords' if search else 'search_keywords'
    db.import_keywords(['旧词'],search=search)
    old = db.rows(f'SELECT * FROM {table}')[0]

    assert db.import_keywords(['旧词','新词',' 新词 ','另一词'],search=search) == {'added':2,'duplicates':2}
    assert db.rows(f'SELECT * FROM {table} ORDER BY id')[0] == old
    assert [r['keyword'] for r in db.rows(f'SELECT * FROM {table} ORDER BY id')] == ['旧词','新词','另一词']
    assert db.rows(f'SELECT * FROM {other}') == []


@pytest.mark.parametrize('search',[False,True])
def test_invalid_later_value_never_leaves_a_partial_import(tmp_path,search):
    db = DB(tmp_path/'test.sqlite3')
    with pytest.raises(ValueError):
        db.import_keywords(['有效词','含\n换行'],search=search)
    assert db.rows('SELECT * FROM keywords') == []
    assert db.rows('SELECT * FROM search_keywords') == []


def test_downloaded_and_bundled_templates_share_one_format(tmp_path):
    path = tmp_path/'template.csv'
    write_keyword_template(path)
    bundled = Path(__file__).resolve().parents[1]/'templates'/'关键词搜索词批量导入模板.csv'
    assert path.read_bytes().startswith(b'\xef\xbb\xbf')
    assert bundled.read_bytes() == path.read_bytes()
    assert read_keyword_csv(path) == list(KEYWORD_CSV_EXAMPLES)
    db = DB(tmp_path/'test.sqlite3')
    assert db.import_keywords(read_keyword_csv(path))['added'] == 3
    assert db.import_keywords(read_keyword_csv(path),search=True)['added'] == 3
