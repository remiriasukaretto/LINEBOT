import http from 'k6/http';
import { check } from 'k6';
import { Rate, Counter } from 'k6/metrics';
import crypto from 'k6/crypto';

const BASE_URL = __ENV.BASE_URL;
const CHANNEL_SECRET = __ENV.CHANNEL_SECRET;
const RESERVATION_TYPE = __ENV.RESERVATION_TYPE || '相談';
const RUN_ID = __ENV.RUN_ID || String(Date.now());

if (!BASE_URL) {
    throw new Error('BASE_URL is not set');
}

if (!CHANNEL_SECRET) {
    throw new Error('CHANNEL_SECRET is not set');
}

export const reservationSuccessRate = new Rate('reservation_success_rate');
export const reservationRequests = new Counter('reservation_requests');
export const callbackAcceptedRate = new Rate('callback_accepted_rate');

export const options = {
    scenarios: {
        reservation_100rps: {
            executor: 'constant-arrival-rate',
            rate: 100,
            timeUnit: '1s',
            duration: '10m',
            preAllocatedVUs: 150,
            maxVUs: 600,
            gracefulStop: '30s',
        },
    },
    thresholds: {
        http_req_failed: ['rate<0.01'],
        http_req_duration: ['p(95)<1000', 'p(99)<2000'],
        reservation_success_rate: ['rate>0.99'],
        dropped_iterations: ['count==0'],
    },
};

function buildEvent(userId, eventId) {
    return {
        destination: 'Uloadtestdestination',
        events: [
            {
                type: 'message',
                mode: 'active',
                timestamp: Date.now(),
                source: {
                    type: 'user',
                    userId,
                },
                webhookEventId: eventId,
                deliveryContext: { isRedelivery: false },
                replyToken: `loadtest-reply-${eventId}`,
                message: {
                    type: 'text',
                    id: eventId,
                    text: `予約 ${RESERVATION_TYPE}`,
                    quoteToken: `loadtest-quote-${eventId}`,
                },
            },
        ],
    };
}

export default function () {
    const eventId = `${__VU}-${__ITER}-${Date.now()}`;
    const userId = `Uloadtest${RUN_ID}${__VU}${__ITER}`;
    const body = JSON.stringify(buildEvent(userId, eventId));
    const signature = crypto.hmac('sha256', CHANNEL_SECRET, body, 'base64');

    const response = http.post(`${BASE_URL}/callback`, body, {
        headers: {
            'Content-Type': 'application/json',
            'X-Line-Signature': signature,
        },
        tags: { scenario: 'reservation_100rps' },
    });

    reservationRequests.add(1);
    const accepted = response.status === 200;
    callbackAcceptedRate.add(accepted);
    reservationSuccessRate.add(accepted);

    check(response, {
        'callback accepted': (r) => r.status === 200,
    });

    if (!accepted) {
        console.log(
            `ERROR status=${response.status} error=${response.error} body=${response.body}`
        );
    }
}
