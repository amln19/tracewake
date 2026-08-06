package awstest

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"time"
)

type queuedMessage struct {
	ID            string
	ReceiptHandle string
	Body          string
	VisibleAt     time.Time
	Received      int
}

// SQS is a single standard queue with visibility timeouts and at-least-once
// delivery.
type SQS struct {
	Server   *httptest.Server
	QueueURL string

	mutex    sync.Mutex
	messages []*queuedMessage
	sent     int
	failNext bool
}

func NewSQS() *SQS {
	fake := &SQS{}
	fake.Server = httptest.NewServer(http.HandlerFunc(fake.serve))
	fake.QueueURL = fake.Server.URL + "/000000000000/locus-jobs"
	return fake
}

func (q *SQS) Close() { q.Server.Close() }

// FailNextSend makes one publish attempt fail, which is how a queue outage
// looks to the outbox publisher.
func (q *SQS) FailNextSend() {
	q.mutex.Lock()
	defer q.mutex.Unlock()
	q.failNext = true
}

func (q *SQS) Bodies() []string {
	q.mutex.Lock()
	defer q.mutex.Unlock()
	bodies := make([]string, 0, len(q.messages))
	for _, message := range q.messages {
		bodies = append(bodies, message.Body)
	}
	return bodies
}

func (q *SQS) serve(w http.ResponseWriter, r *http.Request) {
	target := r.Header.Get("X-Amz-Target")
	var request map[string]any
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		q.fail(w, "SerializationException")
		return
	}
	switch {
	case strings.HasSuffix(target, "SendMessage"):
		q.send(w, request)
	case strings.HasSuffix(target, "ReceiveMessage"):
		q.receive(w, request)
	case strings.HasSuffix(target, "DeleteMessage"):
		q.delete(w, request)
	case strings.HasSuffix(target, "ChangeMessageVisibility"):
		q.changeVisibility(w, request)
	default:
		q.fail(w, "UnknownOperationException")
	}
}

func (q *SQS) fail(w http.ResponseWriter, code string) {
	w.Header().Set("Content-Type", "application/x-amz-json-1.0")
	w.WriteHeader(http.StatusBadRequest)
	_ = json.NewEncoder(w).Encode(map[string]string{"__type": "com.amazonaws.sqs#" + code, "message": code})
}

func (q *SQS) reply(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/x-amz-json-1.0")
	_ = json.NewEncoder(w).Encode(value)
}

func (q *SQS) send(w http.ResponseWriter, request map[string]any) {
	q.mutex.Lock()
	if q.failNext {
		q.failNext = false
		q.mutex.Unlock()
		q.fail(w, "ServiceUnavailable")
		return
	}
	q.sent++
	body, _ := request["MessageBody"].(string)
	message := &queuedMessage{
		ID:            fmt.Sprintf("message-%d", q.sent),
		ReceiptHandle: fmt.Sprintf("receipt-%d", q.sent),
		Body:          body,
	}
	q.messages = append(q.messages, message)
	q.mutex.Unlock()
	sum := md5.Sum([]byte(body))
	q.reply(w, map[string]string{"MessageId": message.ID, "MD5OfMessageBody": hex.EncodeToString(sum[:])})
}

func (q *SQS) receive(w http.ResponseWriter, request map[string]any) {
	visibility := 30 * time.Second
	if raw, ok := request["VisibilityTimeout"].(float64); ok {
		visibility = time.Duration(raw) * time.Second
	}
	q.mutex.Lock()
	defer q.mutex.Unlock()
	now := time.Now()
	var delivered []map[string]any
	for _, message := range q.messages {
		if message.VisibleAt.After(now) {
			continue
		}
		message.VisibleAt = now.Add(visibility)
		message.Received++
		sum := md5.Sum([]byte(message.Body))
		delivered = append(delivered, map[string]any{
			"MessageId":     message.ID,
			"ReceiptHandle": message.ReceiptHandle,
			"Body":          message.Body,
			"MD5OfBody":     hex.EncodeToString(sum[:]),
		})
		break
	}
	q.reply(w, map[string]any{"Messages": delivered})
}

func (q *SQS) delete(w http.ResponseWriter, request map[string]any) {
	handle, _ := request["ReceiptHandle"].(string)
	q.mutex.Lock()
	remaining := q.messages[:0]
	for _, message := range q.messages {
		if message.ReceiptHandle != handle {
			remaining = append(remaining, message)
		}
	}
	q.messages = remaining
	q.mutex.Unlock()
	q.reply(w, map[string]any{})
}

func (q *SQS) changeVisibility(w http.ResponseWriter, request map[string]any) {
	handle, _ := request["ReceiptHandle"].(string)
	timeout, _ := request["VisibilityTimeout"].(float64)
	q.mutex.Lock()
	for _, message := range q.messages {
		if message.ReceiptHandle == handle {
			message.VisibleAt = time.Now().Add(time.Duration(timeout) * time.Second)
		}
	}
	q.mutex.Unlock()
	q.reply(w, map[string]any{})
}
