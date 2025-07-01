import pandas as pd
import numpy as np
from collections import defaultdict
import psycopg2 # PostgreSQL 데이터베이스 연결 라이브러리
import redis    # Redis 연결 라이브러리
import json     # JSON 데이터 처리
from datetime import datetime, timedelta
import os       # 환경 변수 사용을 위해
from dotenv import load_dotenv

load_dotenv()

# --- 1. 데이터베이스와 레디스 연결 함수 ---
DATABASE_URL = os.environ.get('DATABASE_URL')
def get_db_connection():
    """PostgreSQL 데이터베이스 연결을 설정하고 반환합니다. POSTGRES_URL을 지원합니다."""
    try:
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL 환경 변수가 설정되지 않았습니다.")

        conn = psycopg2.connect(DATABASE_URL)
        print(f"[{datetime.now()}] PostgreSQL 연결 성공.")
        return conn
    except Exception as e:
        print(f"[{datetime.now()}] PostgreSQL 연결 실패: {e}. DATABASE_URL을 확인해주세요.")
        raise # 연결 실패 시 예외를 다시 발생시켜 스크립트 중단
        

REDIS_URL = os.environ.get('REDIS_URL')
def get_redis_connection():
    """Redis 서버 연결을 설정하고 반환합니다. Redis URL을 지원합니다."""
    try:
        if not REDIS_URL:
  
            raise ValueError("REDIS_URL 환경 변수가 설정되지 않았습니다. crontab 설정을 확인해주세요.")
            
        r = redis.from_url(
            REDIS_URL,
            decode_responses=True # Redis에서 가져온 데이터를 자동으로 문자열로 디코딩
        )
        
        # Redis 연결 테스트 (ping)
        r.ping()
        print(f"[{datetime.now()}] Redis 연결 성공.")
        return r
    except Exception as e:
        print(f"[{datetime.now()}] Redis 연결 실패: {e}. REDIS_URL을 확인해주세요.")
        raise # 연결 실패 시 예외를 다시 발생시켜 스크립트 중단

# --- 2. Jaccard 유사도 계산 함수 ---
def calculate_jaccard_similarity(co_occurrence_matrix, item_counts):
    """
    아이템 간의 Jaccard 유사도를 계산합니다.
    Args:
        co_occurrence_matrix (pd.DataFrame): 아이템 간의 공동 발생 횟수 매트릭스
        product_counts (dict): 각 아이템의 총 발생 횟수 딕셔너리
    Returns:
        pd.DataFrame: Jaccard 유사도 매트릭스
    """
    similarity_matrix = pd.DataFrame(0.0, index=co_occurrence_matrix.index, columns=co_occurrence_matrix.columns)
    
    # 모든 아이템 쌍에 대해 반복
    for i in co_occurrence_matrix.index:
        for j in co_occurrence_matrix.columns:
            if i == j: # 자기 자신과의 유사도는 계산하지 않음
                continue

            # 교집합 크기
            intersection = co_occurrence_matrix.loc[i, j] 
            
            # 합집합 크기 (|A| + |B| - |A ∩ B|)
            # item_counts[i]는 아이템 i가 등장한 총 카트 수
            union = item_counts[i] + item_counts[j] - intersection
            
            if union > 0:
                similarity_matrix.loc[i, j] = intersection / union
            else:
                similarity_matrix.loc[i, j] = 0.0 # 합집합이 0이면 (둘 다 등장하지 않으면) 유사도 0

    print(f"[{datetime.now()}] Jaccard 유사도 계산 완료.")
    return similarity_matrix

# --- 3. 메인 배치 처리 로직 ---
def main():
    print(f"[{datetime.now()}] 추천 시스템 배치 처리 시작.")
    
    conn = None      
    cursor = None        
    redis_conn = None 
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        redis_conn = get_redis_connection()

        # 3.1. 데이터 추출: 최근 N일 간의 카트 아이템 데이터 가져오기
        days_to_look_back = 30
        start_date = datetime.now() - timedelta(days=days_to_look_back)
        
        print(f"[{datetime.now()}] 지난 {days_to_look_back}일 간의 카트 데이터 조회 시작...")
        query = f"""
            SELECT "cartId", "productId"
            FROM "CartItem"
            WHERE "createdAt" >= '{start_date.isoformat()}'
            ORDER BY "cartId", "createdAt";
        """
        cursor.execute(query)
        cart_data = cursor.fetchall() # (cartId, productId) 튜플의 리스트로 받아옴
        
        if not cart_data:
            print(f"[{datetime.now()}] 조회된 카트 데이터가 없습니다. 배치 처리를 종료합니다.")
            return

        print(f"[{datetime.now()}] 총 {len(cart_data)}개의 카트 데이터 조회 완료.")

        # 3.2. 데이터 전처리: 세션별 아이템 집합 생성 및 아이템 등장 횟수 집계
        carts_products = defaultdict(set) # 각 세션 ID에 해당하는 아이템 ID들의 집합
        all_product_ids = set()             # 전체 고유 아이템 ID 집합
        
        for cartId, productId in cart_data:
            carts_products[cartId].add(productId)
            all_product_ids.add(productId)
        
        all_product_ids_list = sorted(list(all_product_ids)) # 정렬된 리스트로 변환하여 매트릭스 인덱스/컬럼으로 사용

        # 공동 발생 매트릭스 초기화
        co_occurrence_matrix = pd.DataFrame(0, index=all_product_ids_list, columns=all_product_ids_list)
        product_counts = defaultdict(int) # 각 아이템이 등장한 총 세션 수

        for cartId, products_in_cart in carts_products.items():
            for product_a in products_in_cart:
                product_counts[product_a] += 1 # 아이템 A가 포함된 세션 수 증가
                for product_b in products_in_cart:
                    co_occurrence_matrix.loc[product_a, product_b] += 1 # 아이템 A, B가 함께 등장한 세션 수 증가
        
        print(f"[{datetime.now()}] 데이터 전처리 및 공동 발생 매트릭스 생성 완료. 총 고유 아이템 수: {len(all_product_ids_list)}")

        # 3.3. 유사도 계산
        # 여기서는 Jaccard 유사도 함수를 호출합니다. Lift를 사용하고 싶다면 해당 함수로 변경하세요.
        similarity_matrix = calculate_jaccard_similarity(co_occurrence_matrix, product_counts)
        
        # 3.4. 각 아이템별 Top 5 추천 아이템 선정
        recommendations_for_db = []     # DB 저장을 위한 데이터 리스트
        recommendations_for_redis = {}  # Redis 저장을 위한 딕셔너리

        for item_id in all_product_ids_list:
            # 해당 item_id와 다른 아이템들 간의 유사도 점수 가져오기
            # .drop(item_id, errors='ignore')는 자기 자신과의 유사도(주로 1.0)를 제외합니다.
            # .nlargest(5)는 가장 큰 값 5개를 가져옵니다.
            top_5_recs = similarity_matrix.loc[item_id].drop(item_id, errors='ignore').nlargest(5)
            
            # 추천 아이템 목록을 JSON 형식으로 준비
            # ID와 유사도 점수를 함께 저장하여 나중에 활용 가능하도록 합니다.
            # 유사도 점수가 0보다 큰 경우만 유효한 추천으로 간주합니다.
            recommended_items_list = [
                {"id": int(rec_id), "score": round(float(score), 4)} # 소수점 4자리까지 반올림
                for rec_id, score in top_5_recs.items() if score > 0
            ]
            
            # DB 저장을 위한 형식 (item_id, JSON 문자열, 현재 시간)
            recommendations_for_db.append((
                item_id,
                json.dumps({"items": recommended_items_list}), # 파이썬 딕셔너리를 JSON 문자열로 변환
                datetime.now()
            ))
            
            # Redis 저장을 위한 형식 (item_id: JSON 문자열)
            recommendations_for_redis[f"item:recommendation:{item_id}"] = json.dumps({"items": recommended_items_list})

        print(f"[{datetime.now()}] 각 아이템별 Top 5 추천 아이템 선정 완료.")

        # 3.5. PostgreSQL에 추천 결과 저장 (UPSERT)
        # item_recommendations 테이블에 데이터 삽입 또는 업데이트
        print(f"[{datetime.now()}] PostgreSQL에 추천 결과 저장 시작...")
        
        # ON CONFLICT (item_id) DO UPDATE: item_id가 이미 존재하면 업데이트, 없으면 삽입
        insert_update_query = """
            INSERT INTO item_recommendations (item_id, recommended_items, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (item_id) DO UPDATE SET
                recommended_items = EXCLUDED.recommended_items,
                updated_at = EXCLUDED.updated_at;
        """
        cursor.executemany(insert_update_query, recommendations_for_db)
        conn.commit() # 변경사항 커밋
        print(f"[{datetime.now()}] PostgreSQL 저장 완료.")

        # 3.6. Redis에 추천 결과 캐싱
        # Redis 파이프라인을 사용하면 여러 명령을 한 번에 보내 성능을 최적화할 수 있습니다.
        print(f"[{datetime.now()}] Redis에 추천 결과 캐싱 시작...")
        pipe = redis_conn.pipeline()
        for key, value in recommendations_for_redis.items():
            pipe.set(key, value)
            # 선택 사항: Redis 키에 만료 시간(TTL) 설정 (예: 2일 = 172800초)
            # pipe.expire(key, 172800) 
        pipe.execute() # 파이프라인 실행
        print(f"[{datetime.now()}] Redis 캐싱 완료.")

    except Exception as e:
        print(f"[{datetime.now()}] 오류 발생: {e}")
        if conn:
            conn.rollback() # 오류 발생 시 DB 변경사항 롤백
    finally:
        # 연결 닫기 (항상 실행되어야 함)
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        if redis_conn:
            redis_conn.close()
        print(f"[{datetime.now()}] 배치 처리 종료.")

if __name__ == "__main__":
    main()