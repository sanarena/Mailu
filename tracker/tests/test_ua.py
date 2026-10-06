"""Unit tests for User-Agent client/device sniffing."""
import sys
import unittest

sys.path.insert(0, ".")

from tracker.mail_tracker.ua import parse_user_agent


class UaTest(unittest.TestCase):
    def test_gmail_proxy(self):
        client, device = parse_user_agent(
            'Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0'
            ' (via ggpht.com GoogleImageProxy)')
        self.assertEqual(client, 'Gmail image proxy')
        self.assertIn('proxied', device)

    def test_generic_prefetch(self):
        self.assertEqual(parse_user_agent('Mozilla/5.0')[0],
                         'Generic prefetch proxy')

    def test_outlook_desktop(self):
        client, device = parse_user_agent(
            'Microsoft Outlook 16.0 (Windows NT 10.0)')
        self.assertEqual((client, device), ('Outlook', 'Windows PC'))

    def test_outlook_mobile(self):
        self.assertEqual(
            parse_user_agent('Outlook-iOS/4.0 (iPhone; iOS 17)')[0],
            'Outlook iOS')

    def test_thunderbird_linux(self):
        client, device = parse_user_agent(
            'Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101'
            ' Thunderbird/115.0')
        self.assertEqual((client, device), ('Thunderbird', 'Linux PC'))

    def test_apple_mail_mac(self):
        client, device = parse_user_agent(
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit'
            ' (KHTML, like Gecko)')
        self.assertEqual(device, 'Mac')

    def test_iphone(self):
        client, device = parse_user_agent(
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)')
        self.assertEqual(device, 'iPhone')

    def test_unknown(self):
        self.assertEqual(parse_user_agent(''), ('Unknown', 'Unknown'))
        self.assertEqual(parse_user_agent('curl/8.0')[0], 'Unknown')


if __name__ == '__main__':
    unittest.main()
