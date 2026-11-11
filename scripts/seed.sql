-- mysqlagent 演示数据库：模拟电商业务数据
-- 库：mysqlagent_demo（owner: mysqlagent）

BEGIN;

-- 商品分类
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    comment     TEXT DEFAULT ''
);
COMMENT ON TABLE categories IS '商品分类';

-- 商品
CREATE TABLE IF NOT EXISTS products (
    id          SERIAL PRIMARY KEY,
    category_id INT NOT NULL REFERENCES categories(id),
    name        TEXT NOT NULL,
    price       NUMERIC(10,2) NOT NULL,
    stock       INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE products IS '商品信息';
COMMENT ON COLUMN products.price IS '售价（元）';

-- 用户
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    city        TEXT NOT NULL,
    level       SMALLINT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE users IS '注册用户';
COMMENT ON COLUMN users.level IS '会员等级 1-5';

-- 订单
CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id),
    status      TEXT NOT NULL,
    amount      NUMERIC(10,2) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE orders IS '订单主表';
COMMENT ON COLUMN orders.status IS '状态: pending/paid/shipped/completed/cancelled';

-- 订单明细
CREATE TABLE IF NOT EXISTS order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INT NOT NULL REFERENCES orders(id),
    product_id  INT NOT NULL REFERENCES products(id),
    quantity    INT NOT NULL,
    unit_price  NUMERIC(10,2) NOT NULL
);
COMMENT ON TABLE order_items IS '订单明细';

-- 清空旧数据（幂等）
TRUNCATE order_items, orders, products, categories, users RESTART IDENTITY CASCADE;

-- 分类
INSERT INTO categories (name, comment) VALUES
    ('手机数码', '手机、平板、智能穿戴'),
    ('电脑办公', '笔记本、台式机、显示器'),
    ('外设配件', '键盘、鼠标、耳机'),
    ('家用电器', '大家电与小家电');

-- 商品（24 个）
INSERT INTO products (category_id, name, price, stock, created_at)
SELECT
    1 + (g % 4),
    (ARRAY[
        '星辰手机 Pro 256G', '星辰手机 Ultra 512G', '平板 Air 11 英寸', '智能手表 S7',
        '轻薄本 14 英寸 32G', '游戏本 16 英寸 RTX', '4K 显示器 27 英寸', '迷你主机 i9',
        '机械键盘 87 键', '无线鼠标静音款', '降噪耳机 Pro', '蓝牙音箱 mini',
        '变频空调 1.5 匹', '滚筒洗衣机 10KG', '扫地机器人 X20', '空气净化器 H13',
        '快充充电器 100W', '氮化镓数据线', '扩展坞 8 合 1', '显示器支架臂',
        '游戏手柄 Pro', '摄像头 4K', 'USB 麦克风', '便携投影仪'
    ])[g],
    ROUND((99 + random() * 8900)::numeric, 2),
    10 + (random() * 500)::int,
    now() - (g || ' days')::interval
FROM generate_series(1, 24) g;

-- 用户（50 个）
INSERT INTO users (name, email, city, level, created_at)
SELECT
    (ARRAY['王','李','张','刘','陈','杨','赵','黄','周','吴','徐','孙','马','朱','胡','郭','何','林'])[1 + g % 18]
        || (ARRAY['伟','芳','娜','敏','静','磊','军','洋','勇','艳','杰','涛','明','超','秀英','霞','平','刚','桂英'])[1 + g % 19],
    'user' || g || '@demo.com',
    (ARRAY['上海','北京','深圳','杭州','成都','广州','南京','武汉','西安','苏州'])[1 + g % 10],
    1 + (random() * 5)::int,
    now() - (g || ' days')::interval
FROM generate_series(1, 50) g;

-- 订单（300 单，近 90 天）
INSERT INTO orders (user_id, status, amount, created_at)
SELECT
    1 + (random() * 49)::int,
    (ARRAY['pending','paid','paid','shipped','shipped','completed','completed','completed','cancelled'])[1 + (random() * 8)::int],
    ROUND((29 + random() * 9800)::numeric, 2),
    now() - (random() * 90 || ' days')::interval
FROM generate_series(1, 300) g;

-- 订单明细（每单 1-3 件，金额与订单头保持一致由应用层保证，此处独立生成明细）
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT
    o.id,
    1 + (random() * 23)::int,
    1 + (random() * 2)::int,
    p.price
FROM orders o
CROSS JOIN LATERAL (SELECT price FROM products ORDER BY random() LIMIT 1) p
WHERE random() < 0.9;  -- 约 10% 订单暂无明细

COMMIT;

-- 校验数据量
SELECT 'categories' AS tbl, count(*) FROM categories
UNION ALL SELECT 'products', count(*) FROM products
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'order_items', count(*) FROM order_items;
