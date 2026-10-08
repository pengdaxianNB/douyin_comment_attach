"""Shared CSV format for index keywords and Douyin search terms."""
import csv
import io
from pathlib import Path


KEYWORD_CSV_HEADER = '关键词'
KEYWORD_CSV_EXAMPLES = ('关键词示例1','关键词示例2','关键词示例3')
KEYWORD_CSV_HEADERS = {'关键词','搜索词','搜索关键词','keyword'}


def read_keyword_csv(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            text = raw.decode('gb18030')
        except UnicodeDecodeError as exc:
            raise ValueError('CSV 编码无法识别，请另存为 CSV UTF-8 后重试') from exc
    if '\x00' in text:
        raise ValueError('CSV 含无效字符，请使用通用模板并另存为 CSV UTF-8')
    reader = csv.reader(io.StringIO(text,newline=''),strict=True)
    values = []
    header_found = False
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            # Excel may export extra empty columns. Nonempty extra fields are
            # rejected so unquoted commas never silently truncate a keyword.
            if any(cell.strip() for cell in row[1:]):
                raise ValueError(f'CSV 第 {reader.line_num} 行有多列内容，请按模板每行只填一个词；词内含英文逗号时请用双引号包裹')
            value = row[0].strip()
            if not header_found:
                if value.lower() not in KEYWORD_CSV_HEADERS:
                    raise ValueError('CSV 第一行表头应为“关键词”（也支持“搜索词”“搜索关键词”或 keyword），请使用通用模板')
                header_found = True
                continue
            if '\n' in value or '\r' in value:
                raise ValueError(f'CSV 第 {reader.line_num} 行的词包含换行，请每行填写一个词')
            if value:
                values.append(value)
    except csv.Error as exc:
        raise ValueError(f'CSV 第 {reader.line_num} 行格式错误：{exc}') from exc
    if not values:
        raise ValueError('CSV 没有可导入的词，请在表头下方每行填写一个词')
    return values


def write_keyword_template(path):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow([KEYWORD_CSV_HEADER])
        writer.writerows([value] for value in KEYWORD_CSV_EXAMPLES)
